# Memoria del proyecto

## 2026-10-22 — Actualización del Dell

- Máquina verificada: DELL, Dell Latitude 5400. La copia activa es `C:\Proyectos\ai-usage-monitor`; `C:\Users\Usuario\ai-usage-monitor` es otra copia y no ejecuta el widget.
- `main` de la copia activa avanzó por fast-forward desde `6ca4e4d` a `64b334f3639fefdb05a3c0a6407e4d345f4f1f04`, de `https://github.com/Mchicao/ai-usage-monitor.git`.
- Respaldo con hashes comprobados, patch, bundle y originales: `.cache/update-20261022-151921/`. El stash original se conserva; su identificador está en `stash-ref.txt` dentro del respaldo.
- Se conservaron las adaptaciones del Dell: Python de `.venv`, lanzador VBS, credenciales Windows/Cloud Code como fallback, descubrimiento Z.AI por entorno/archivo/OpenCode, colapso conservador de aliases y protección de instancia única. Las mejoras remotas de AGY/Argon, cuentas Codex, resets y actualización del popup permanecen activas.
- `dashboard_server.py`, `get_antigravity_quota.py`, `launch_tray_widget.vbs` y `test_quota_sources.py` conservan exactamente su contenido local anterior. Los archivos de credenciales comprobados mantuvieron sus hashes. No se cambió la tarea programada `AI Usage Monitor` ni el entorno virtual.
- Reinicio limitado al PID 13256, comprobando lock, ruta del script y dueño del puerto antes de detenerlo. Se usó el mismo lanzador VBS para iniciar el widget actualizado.
- Validación: 32 tests de `test_codex_multi_account` y `test_quota_sources`; sintaxis de todos los Python y `git diff --check` correctos. Ruff no detectó infracciones nuevas; se excluyeron únicamente E722 de `dashboard_server.py` y F401 de `win_tray_widget.py`, ambos comprobados también en el respaldo previo. Basedpyright no estaba disponible en el cache offline y no se instaló.
- El verificador upstream `verify-widget-quotas.py` presupone una segunda cuenta OpenCode distinta. Este Dell tiene una única cuenta deduplicada con etiqueta Codex. La comprobación de la máquina está en `.cache/verify-dell-widget.py`; su informe está en `.cache/verify-dell-widget.json`.
- La comprobación del popup verifica las tarjetas con datos del widget reiniciado. No acredita conservación del foco: esa aserción se retiró del verificador temporal porque el escritorio está compartido e interactivo.
- No se realizó push, commit ni cambio en otros servicios. Los cambios locales quedan sin commit y el respaldo permanece disponible.
- Verificación final correcta: proceso del widget PID 17860, dueño de 127.0.0.1:8420 y coincidente con el lock. AGY (Gemini, Claude/GPT y Argon), Codex y Z.AI con estado ok; tarjetas del popup actualizadas. Refresco automático comprobado entre 18:25:13 y 18:27:19 UTC. Informe final: `.cache/verify-dell-widget.json`.

## 2026-10-27 — Corrección Z.AI mediante sesión ZCode

- Usuario autorizó arreglar Python y Rust. Se aplicó un parche focalizado a ai_quota_widget.py y win_tray_widget.py, se añadió scripts/utils/zcode_session.py y se actualizó la expectativa de fechas en test_codex_multi_account.py. Otras adaptaciones Dell, lanzador VBS y .venv se conservaron.
- La sesión ZCode se descifra en memoria con Windows CNG. Se confirma familia Z.ai, identidad y Coding Plan personal. Cuota y tarjetas tienen errores independientes; el popup incluye MCP y tarjetas agrupadas por vencimiento con fecha/hora local. No se cambió ninguna credencial.
- Respaldos con hashes y candidato revisable en C:\Users\Usuario\ai-usage-monitor\.cache\zai-integration-20261027-121503 y .cache\python-zai-candidate. Rust se desarrolla en ese otro checkout; aquí solo se ejecuta Python.
- Validación: 31 pruebas de esta copia y 7 focalizadas del checkout de desarrollo correctas; Ruff correcto. Se excluyó la prueba de caché AGY test_antigravity_cache_failure_is_not_reported_as_fresh_ok: se comprobó que ya fallaba en esta copia original antes del parche. No declarar suite completa verde.
- Python reiniciado con el mismo VBS: PID efectivo 14912, lock y dueño de 127.0.0.1:8420 confirmados. Endpoint real devuelve Z.AI ok, source ZCode, plan Lite, 100% 5h, 71% MCP y 7 tarjetas.
- Rust de prueba actualizado en el otro checkout: PID 16972 desde target/release, con --auto-redeem-resets. Rust es el único propietario del canje automático Codex/Z.AI; Python solo presenta datos. No se consumieron tarjetas durante validación ni se ejecutó benchmark. Revisión visual manual pendiente.
