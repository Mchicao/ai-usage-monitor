# Memoria del proyecto

## 2026-10-27 — Preparación de comparación Python/Rust

- Checkout solicitado: `C:\Users\Usuario\ai-usage-monitor`, remoto `https://github.com/Mchicao/ai-usage-monitor.git`. `main` sincronizado por fast-forward desde `64b334f` a `329d98ebb9180772cd536d88d3de53e4c09fc97b`; no había cambios locales versionados.
- Antes del arranque, se identificó otro widget Python en `C:\Proyectos\ai-usage-monitor`: proceso efectivo PID 17860, lanzador PID 16220; el PID efectivo coincide con el lock y ocupa `127.0.0.1:8420`.
- Las dos implementaciones usan el puerto fijo 8420. Python falla si no puede abrirlo; Rust omite su servidor cuando no puede abrirlo, pero su bandeja y popup nativos son independientes. Se preguntó al usuario si conservar el Python existente o detenerlo para iniciar el Python actualizado de este checkout. No detener la instancia existente sin respuesta.
- Dependencias de interfaz verificadas mediante imports en `C:\Proyectos\ai-usage-monitor\.venv\Scripts\python.exe`: customtkinter 6.0.0, pystray 0.19.5 y Pillow 12.3.0. El widget y su módulo de cuotas no importan requests.
- `cargo build --release` terminó con código 0: `Finished release profile [optimized]` en 2m 31s. No se iniciaron widgets nuevos ni se ejecutó el benchmark, a la espera de resolver la instancia Python existente.
- Advertencia de implementación: Rust escribe su log en la ruta fija `C:\Proyectos\ai-usage-monitor\debug.log`.
- El usuario exige detener todos los comandos tras verificar ambos procesos y esperar su confirmación manual de los popups antes de medir CPU/RAM.

### Continuación — Arranque autorizado

- El usuario eligió conservar el Python activo de `C:\Proyectos\ai-usage-monitor` y abrir Rust para probar sus popups desde la bandeja. No reiniciar ni sustituir el Python existente.
- Rust release de este checkout se lanzó en segundo plano con `Start-Process -WindowStyle Hidden`, PID asignado 22200. Python efectivo previamente identificado: PID 17860 (lanzador del entorno virtual: 16220).
- El siguiente y último comando de esta fase comprueba el registro de inicialización de la bandeja Rust y la presencia de ambos procesos. Después se deben suspender todos los comandos hasta la confirmación manual del usuario. No se ejecutó el benchmark.

### Correcciones tras prueba manual — Z.AI y resets

- El usuario informó ausencia de Z.AI y pidió fechas/horas de resets y canje automático. Aclaró explícitamente que desea aplicar resets manuales antes de que caduquen, no generar tareas para gastar cuota.
- Reproducción: copia actual no encontraba platform token. La copia Python activa aceptaba HTTP 200 con `success=false` y terminaba con nivel unknown/límites vacíos. API key local, exportado por OpenCode y configuración ZCode consultada devuelven `Authentication Failed`, código 1000. No se imprimieron ni cambiaron secretos. La tarjeta privada de solicitud de API key venció sin respuesta.
- Corregidos en este checkout: validación de respuestas Z.AI y errores visibles; lectura de API key de entorno/OpenCode; preservación de timestamps; fechas/horas locales de resets. Rust obtiene el detalle Codex desde el endpoint de créditos. Contador autoritativo conservado aunque falten filas.
- Consulta real Codex confirmó dos vencimientos: `29/10 14:00 UTC-03:00` y `06/11 19:54 UTC-03:00`.
- Canje automático Codex implementado en `ai-usage-monitor-rs/src/auto_reset.rs`, opt-in con `--auto-redeem-resets`. Comprueba cada 60s el crédito que vence primero dentro de 10 minutos; revalida ID/estado/vencimiento con la cuenta actual del Codex App Server, usa una clave idempotente estable y refresca cuotas. Python solo lee. No se canjeó ningún reset durante las pruebas; el canje real no se ha verificado.
- 28 pruebas Python y 4 Rust correctas, Ruff correcto con exclusión F401 preexistente limitada a win_tray_widget.py, cargo check y build release correctos. `--check-reset-access` del binario nuevo terminó con código 0 y registro de 2 resets disponibles mediante lectura del Codex instalado 0.164.0.
- Se conservó la copia Python activa sin editar ni reiniciar, PID efectivo 17860. Se detuvo solo el Rust de prueba PID 22200 y se lanzó la compilación en `target/validation/release/` con canje habilitado, PID 17016. El ejecutable anterior en `target/release/` queda como rollback. Verificación final: ambos procesos activos, Rust con argumento de canje, registro de bandeja BOOL(1) y bucle Win32 activo; snapshot nuevo con Codex ok/2 resets y Z.AI error 1000. Presentación visual pendiente de prueba manual.
- Pendientes: renovación Z.AI, detalle/canje de tarjetas manuales Z.AI, aplicar cambios a copia Python activa y prueba visual manual de los popups. No se ejecutó benchmark ni push. Guía: `docs/guides/resets.md`; plan vivo: `docs/codex/plans/PLAN_CUOTAS_Y_RESETS.md`.

### Investigación ZCode — Corrección del diagnóstico de autenticación

- El usuario indicó que ZCode está abierto y pidió investigar otros medidores. Alcance de esta continuación: investigación y consultas GET; sin editar código de aplicación, reiniciar widgets ni canjear resets.
- Se verificó ZCode instalado 3.16.3 en `C:\Program Files\ZCode`. Su `resources/app.asar` contiene el lector oficial del almacén `~/.zcode/v2/credentials.json`, cifrado AES-256-GCM `enc:v1:`. Se reprodujo la lectura en memoria con Node crypto y la derivación del código instalado; no se imprimieron ni persistieron secretos.
- La sesión NO necesita renovarse para leer cuota: `oauth:zai:access_token` descifrado devolvió HTTP 200, `code=200`, `success=true` en GET `https://api.z.ai/api/monitor/usage/quota/limit` (tanto raw como Bearer). Plan Lite: TOKENS_LIMIT 0% usado, ventana unit=3/number=5 sin nextResetTime; TIME_LIMIT 29% usado, 29/100, unit=5/number=1 y nextResetTime=1795008964984. No inventar reset de la ventana 5h si el servicio no lo devuelve.
- El API key de Coding Plan asociado exactamente al user_id de la sesión sigue dando código 1000. Por tanto, la conclusión anterior de que faltaba una sesión válida era demasiado amplia: se estaba consultando con otra clase de credencial y no se leía la sesión ZCode.
- GET `https://zcode.z.ai/api/v1/coding-plan/reset/status` con zcodejwttoken en Authorization Bearer, oauth:zai:access_token en X-Bigmodel-Authorization y Bigmodel-Target-Type PERSONAL devolvió code=0. Hay 7 tarjetas de reset 5h: 4 vencen 28/10/2026 09:59:00 UTC-03:00; 3 vencen 22/12/2026 12:59:59 UTC-03:00. No hay tarjetas semanales.
- El código instalado también documenta POST `/api/v1/coding-plan/reset/use` con `idempotency_key` (no vacío, máximo 64 caracteres) y `reset_type`. No se ejecutó ningún POST. `/reset/opportunity` y `/reset/history/read` tampoco se ejecutaron.
- Evidencia local sin secretos: `.cache/zcode-investigation/local-summary.json`; extractos de código público instalado en esa misma carpeta. La integración de este lector en los widgets sigue pendiente; no confundir la validación de investigación con un arreglo ya desplegado.
- Investigación externa concluida en `docs/technical/INVESTIGACION_ZAI_MEDIDORES.md`, con commits fijados de Token Monitor, Subscription Monitor y ZCode oficial. Se añadió una sección que contrasta los contratos con las consultas reales de este equipo. La entrada `oauth:zai:access_token` del Desktop representa un JWT de plataforma/negocio, no necesariamente el OAuth original. ZCode público 3.16.2 no persiste los PAT actuales; no copiar claves históricas ni emitir PAT como efecto secundario del monitor. El GET con JWT de plataforma sí funcionó aquí y permite una integración pasiva sin nueva API key. No se hicieron cambios adicionales en código de aplicación ni en procesos.

### Integración autorizada en Python y Rust

- El usuario pidió explícitamente arreglar ambas versiones. Se añadieron lectores de sesión `scripts/utils/zcode_session.py` y `ai-usage-monitor-rs/src/zcode_session.rs`: AES-GCM autenticado mediante Windows CNG, sin Node ni paquetes nuevos. Solo familia Z.ai, perfil identificado y selección `individual-coding-plan`; no se mezclan ámbitos o cuentas. No se escriben/exportan credenciales.
- Cuota e inventario de tarjetas mantienen errores independientes. Los popups muestran porcentaje, MCP, reinicios solo si el servicio devuelve timestamp y tarjetas agrupadas por tipo/vencimiento con hora local. CREDIT_LIMIT semanal se diferencia de cinco horas.
- Canje automático Z.AI incorporado al trabajador Rust existente (--auto-redeem-resets). Comprueba cada minuto, exige tarjeta próxima a vencer dentro de 10 min y cuota usada/conocida; relee cuenta, cuota e inventario antes de POST y conserva clave idempotente por tipo/vencimiento/cantidad. El servicio elige la tarjeta por tipo, no admite card_id. Python no canjea para evitar competencia.
- Respaldo de fuentes de ambos checkouts y release anterior: `.cache/zai-integration-20261027-121503/`, con hashes comprobados. El binario Rust anterior en `target/validation/release/` también sigue disponible. Candidato Python y parche revisable: `.cache/python-zai-candidate/`.
- Copia activa C:\Proyectos actualizada solo en ai_quota_widget.py, win_tray_widget.py, test_codex_multi_account.py (expectativa de fechas) y nuevo helper scripts/utils/zcode_session.py. Se preservaron otras adaptaciones y lanzador VBS, credenciales y entorno virtual.
- Validación: 7 pruebas focalizadas Python, 31 de la copia activa y 6 Rust correctas; Ruff y cargo build release --locked correctos. La prueba AGY `test_antigravity_cache_failure_is_not_reported_as_fresh_ok` ya falla en la copia activa original sin el parche; se excluyó sin modificarla. No declarar toda la suite activa verde.
- Reiniciados solo los widgets: Python efectivo PID 14912 (lock/puerto 8420 y ruta confirmados), Rust PID 16972 desde target/release con --auto-redeem-resets y snapshot separado `.cache/rust-validation-snapshot.json`. Ambos devuelven source ZCode, plan Lite, 100% restante 5h, 71% MCP y 7 tarjetas. Rust registró icono BOOL(1) y bucle Win32.
- No se consumió ninguna tarjeta durante validación ni se ejecutó benchmark, push, login o emisión de oportunidades. La presentación de los popups espera confirmación manual del usuario; el canje real no fue ejercitado contra servidores.

### 2026-10-27 — Comparación CPU/RAM autorizada

- El usuario pidió comparar CPU y RAM. Se confirmó por ruta/command line/lock: Python principal 14912 y lanzador fijo 24456; Rust release 16972 con auto-redeem activo. Se tomaron 16 muestras por versión durante aproximadamente 15 s, sin cambiar popups, reiniciar widgets ni provocar actualizaciones/canjes.
- Python combinado: Working Set promedio 69.296875 MiB; privada promedio 37.183594 MiB; 12 hilos (10 principal + 2 lanzador); CPU delta 0.203125 s, promedio 0.169427% del total de 8 procesadores lógicos, pico observado por intervalo 0.401281%; CPU acumulada 14.859375 s y uptime principal 10.35 min. Al final, principal WS 63.089844 MiB y lanzador 6.207031 MiB.
- Rust: Working Set promedio 22.308105 MiB; privada promedio 4.674805 MiB; 12 hilos; CPU delta 0 s medidos y promedio/pico 0% en esta ventana; CPU acumulada 1.1875 s y uptime 8.61 min. No interpretar CPU delta cero como consumo absoluto cero en otros intervalos.
- Diferencia observada: Rust 67.81% menos WS y 87.43% menos memoria privada comprometida. Working Set puede contar páginas compartidas; privada comprometida no equivale necesariamente a RAM residente. CPU acumulada incluye periodos de ejecución distintos. No es un ensayo de carga ni cubre necesariamente el ciclo de recolección de 120 s.
- Evidencia: output/benchmarks/20261027-124506/resources.json y samples.csv. Comparación gráfica en consumo-monitores.html; comprobación html_preview sin mensajes de consola. No se modificó código de aplicación.

### 2026-10-27 — Publicación en GitHub y autostart de Rust

- El usuario pidió subir los avances de ambas versiones (Python y Rust) y dejar solo Rust con inicio automático en este Dell.
- Commit publicado en `main` de `https://github.com/Mchicao/ai-usage-monitor.git` con la integración ZCode/Z.AI en Python y Rust, `auto_reset.rs`, `zcode_session.rs`, docs, pruebas y la evidencia del benchmark. Los snapshots locales de cuotas, logs y `.cache/` siguen ignorados por `.gitignore`.
- Autostart de Python retirado: se exportó la tarea `AI Usage Monitor` a `.cache/autostart/AI Usage Monitor (python).xml` y se eliminó del Programador de tareas. No había otras entradas (HKCU/HKLM Run ni carpeta Inicio) que lanzaran Python.
- Autostart nuevo: tarea `AI Usage Monitor Rust`, trigger AtLogOn de `DELL\Usuario`, ejecuta `C:\Users\Usuario\ai-usage-monitor\ai-usage-monitor-rs\target\release\ai-usage-monitor-rs.exe --auto-redeem-resets` con directorio de trabajo `ai-usage-monitor-rs`, RunLevel Limited, reinicio 3×/2 min y sin límite de ejecución.
- Verificación: se detuvo el Rust previo y se arrancó por la tarea; PID 4672 único, icono de bandeja registrado (`Shell_NotifyIconW NIM_MODIFY`) y snapshot con AGY, Codex y Z.AI completos. El widget Python de `C:\Proyectos` sigue corriendo manualmente (PID 14912); ya no arranca solo.
- Pendiente conocido: Rust sigue escribiendo su log en `C:\Proyectos\ai-usage-monitor\debug.log` (ruta fija heredada), aunque se ejecute desde este checkout.
