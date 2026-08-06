# 🤖 Dashboard de Cuota IA — Unificado

Monitoreo en tiempo real del uso restante de tus planes de IA.

## Proveedores soportados

| Proveedor | Método | ¿Cuota restante? | Estado |
|---|---|---|---|
| **Google Antigravity IDE** | API Google Cloud Code (`cloudcode-pa.googleapis.com`) | ✅ `remainingFraction` por modelo | Requiere `antigravity-usage login` (1 vez) |
| **OpenAI Codex ×2** | API de uso con OAuth local de Codex y Hermes | ✅ `used_percent` ventanas 5h/7d por cuenta | ✅ Descubrimiento automático |
| **Z.AI Coding Plan** | Probe HTTP + detección de 429 | ⚠️ Solo disponible/no disponible + límites teóricos | ✅ Funciona sin configuración |

## Instalación rápida

```bash
# 1. Antigravity CLI (opcional, para cuota de Antigravity)
npm install -g antigravity-usage
antigravity-usage login  # una sola vez

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
- **API**: `https://cloudcode-pa.googleapis.com/v1internal:fetchAvailableModels`
- **Auth**: OAuth 2.0 de Google (vía `antigravity-usage login`)
- **Datos**: `remainingFraction` (0.0–1.0) + `resetTime` por modelo
- **Reset**: Cada ~5 horas por modelo (ventana deslizante)

### OpenAI Codex
- **Fuentes OAuth**: `~/.codex/auth.json` y `%LOCALAPPDATA%/hermes/auth.json`
- **API**: consulta de uso autenticada; los tokens sólo se leen en memoria
- **Multi-cuenta**: deduplica por `chatgpt_account_id` y muestra Codex y Hermes por separado
- **Datos**: `used_percent` para ventanas `primary` (5h) y `secondary` (7d)
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
