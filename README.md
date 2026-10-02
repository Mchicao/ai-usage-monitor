# 🤖 Dashboard de Cuota IA — Unificado

Monitoreo en tiempo real del uso restante de tus planes de IA.

## Proveedores soportados

| Proveedor | Método | ¿Cuota restante? | Estado |
|---|---|---|---|
| **Google Antigravity IDE** | CLI oficial `agy --print "/usage"` | ✅ cuotas semanal/5h por familia | Requiere login OAuth de `agy` |
| **OpenAI Codex ×2** | API de uso con OAuth local de Codex y Hermes | ✅ `used_percent` ventanas 5h/7d por cuenta | ✅ Descubrimiento automático |
| **Z.AI Coding Plan** | Probe HTTP + detección de 429 | ⚠️ Solo disponible/no disponible + límites teóricos | ✅ Funciona sin configuración |

## Instalación rápida

```bash
# 1. Login OAuth de Antigravity (una sola vez)
agy --print "/usage"

# 2. CodeZeno — widget nativo en taskbar de Windows
winget install CodeZeno.ClaudeCodeUsageMonitor
```

## Uso

### Script Python (terminal)

```bash
# Salida legible
python ai_quota_widget.py

# JSON para integración
python ai_quota_widget.py --json

# Modo watch (actualización cada 5 min)
python ai_quota_widget.py --watch
```

### Dashboard web visual

```bash
python dashboard_server.py
# Abre automáticamente http://127.0.0.1:8420/
```

### Widget nativo Windows (CodeZeno)

Se inicia automáticamente con Windows. Muestra barras en la taskbar para Claude Code + Codex + Antigravity.

## Detalles técnicos

### Antigravity IDE
- **Fuente**: `agy --print "/usage"`, que expone cuotas separadas por familia y ventana
- **Auth**: OAuth 2.0 de Google gestionado por `agy`
- **Datos**: cuota semanal y de 5 horas para Gemini y Claude/GPT
- **Fallback**: `antigravity-usage quota --refresh`; si falla, el último snapshot se marca como desactualizado y no se presenta como cuota actual

### OpenAI Codex
- **Fuentes OAuth**: `~/.codex/auth.json`, `%LOCALAPPDATA%/hermes/auth.json` y `~/.local/share/opencode/auth.json`
- **API**: consulta de uso autenticada; los tokens sólo se leen en memoria
- **Multi-cuenta**: deduplica por `chatgpt_account_id`; ante la misma cuenta prefiere el token vigente (si el cacheado de un tool expira, usa el de otro tool que siga vivo)
- **Datos**: `used_percent` por ventana (5h/7d). La API cambió de forma: puede exponer solo la semanal como `primary` con `secondary` en null — las ventanas se etiquetan por su duración real (`window_hours`), no por posición
- **También**: `plan_type` (Plus/Pro) y `credits`
- **Fallback**: usa `~/.codex/sessions/**/*.jsonl` si la API no está disponible

### Consumo del widget

- Un único proceso `pythonw.exe` en la bandeja.
- Actualización cada 120 segundos.
- No lanza procesos Python hijos por actualización; sólo `antigravity-usage` cuando consulta Google.

### Z.AI Coding Plan
- **Sin endpoint de billing**: Probados 12+ endpoints → todos 404
- **Estrategia**: Probe HTTP → 200 = disponible, 429 = agotado
- **Cuota real**: Disponible en `z.ai → Usage Statistics` (requiere login web)
- **Límites del plan**:
  - Lite: 80 prompts/5h, 400/semana
  - Pro: 400 prompts/5h, 2000/semana
  - Max: 1600 prompts/5h, 8000/semana

## Cron job de Hermes

Se creó un cron job que reporta el estado cada 2 horas:

```bash
hermes cron list  # ver el job
```

## Archivos

| Archivo | Descripción |
|---|---|
| `ai_quota_widget.py` | Script principal — consulta los 3 proveedores |
| `dashboard_server.py` | Servidor web local con dashboard visual |
| `dashboard.html` | Dashboard HTML con tarjetas y barras de progreso |
| `get_antigravity_quota.py` | Script standalone para Antigravity (API directa) |
