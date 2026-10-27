# Cuotas Z.AI y vencimientos de resets

## Purpose / Big Picture

Mostrar cuota real de Z.AI o un error explícito y los vencimientos individuales de resets manuales de Codex con fecha, hora local y zona. No confundirlos con reinicios periódicos de cuota.

## Progress

- [x] (2026-10-27) Reproducido: checkout sin token Z.AI; copia activa acepta `success=false` como cuota válida vacía. API key guardado y exportado por OpenCode devuelve código 1000.
- [x] (2026-10-27) Confirmado: Rust conserva solo el contador de resets Codex y no consulta su detalle.
- [x] (2026-10-27) Corregida validación Z.AI y estados de error en este checkout; credencial actual rechazada por el servicio, pendiente renovación.
- [x] (2026-10-27) Vencimientos individuales y horas locales conservados en Python y Rust. Consulta real: 29/10 14:00 y 06/11 19:54 UTC-03:00.
- [x] (2026-10-27) 28 pruebas Python, 4 Rust, Ruff y compilación release correctos. Comprobación de acceso Codex instalada: 2 resets, sin canjear.
- [x] (2026-10-27) Usuario aclaró canje de resets manuales. Implementado en Rust para la cuenta actual de Codex, con margen de 10 minutos, comprobación cada minuto y clave estable de idempotencia por crédito.
- [x] (2026-10-27) Rust PID 17016 verificado con `--auto-redeem-resets`, registro de icono `BOOL(1)`, bucle Win32 activo y snapshot nuevo con Codex ok/2 resets; Z.AI conserva el error de autenticación 1000. Python PID 17860 sigue activo.
- [x] (2026-10-27) Investigación confirmó sesión ZCode válida: cuota con JWT de plataforma y tarjetas con JWT ZCode más JWT de plataforma. No hace falta otra API key.
- [x] (2026-10-27) Lectura AES-GCM mediante Windows CNG en Python/Rust, sin Node ni dependencias nuevas. Cuota y tarjetas conservan sus errores independientes.
- [x] (2026-10-27) Popups preparados con porcentaje, MCP y vencimientos agrupados con fecha/hora local. Canje automático Z.AI en Rust: revalida cuenta/cuota/tarjetas y no consume con cuota llena o desconocida.
- [x] (2026-10-27) Parche aplicado a copia Python activa, preservando adaptaciones Dell. Rust release recompilado y ambos widgets reiniciados: Python 14912, Rust 16972 con --auto-redeem-resets.
- [x] (2026-10-27) Endpoint Python y snapshot Rust nuevos: source ZCode, plan Lite, 100% 5h, 71% MCP y 7 tarjetas. Bandeja Rust BOOL(1). Sin canjes reales ni benchmark.
- [x] (2026-10-27) 7 pruebas focalizadas Python, 31 de la copia activa y 6 Rust correctas; Ruff y build release correctos. La prueba AGY de caché ya falla también en la copia original y se excluyó sin modificarla.
- [ ] Presentación visual de los popups pendiente de confirmación manual. Canje real no ejercitado contra los servidores.

## Surprises & Discoveries

El Python activo usa `C:\Proyectos\ai-usage-monitor`, tiene cambios locales y comparte el puerto 8420 con Rust. El usuario autorizó arreglar ambas versiones después de la investigación. Se aplicaron únicamente funciones/imports concretos tras probar una copia candidata y se inició Python antes de Rust para conservar el puerto. HTTP 200 no implica éxito en la API Z.AI.

## Decision Log

El usuario aclaró que desea canjear resets manuales antes de caducar. Se utiliza App Server de Codex instalado: consulta actual del ID/estado/vencimiento antes del canje y clave estable por crédito para reintentos idempotentes. No hay canjes reales durante las pruebas. Se conserva el contador autoritativo aunque el detalle esté limitado. Las horas se convierten con la zona local del sistema; nunca se inventa una hora de vencimiento.

## Outcomes & Retrospective

Correcciones aplicadas en ambos checkouts y verificadas con datos reales de ZCode, sin renovar ni escribir credenciales. Ambos widgets están en ejecución. La integración usa únicamente la familia Z.ai y el Coding Plan personal seleccionado; otros ámbitos fallan explícitamente. El canje automático está centralizado en Rust para evitar competencia con Python. La API Z.AI permite elegir tipo, no ID de tarjeta. Presentación visual y canje real quedan sin verificar; benchmark suspendido hasta confirmación del usuario.

## Context and Orientation

`ai_quota_widget.py` consulta y normaliza proveedores; `win_tray_widget.py` presenta Python. Rust replica la recolección en `src/codex.rs` y `src/zai.rs`, el contrato en `src/models.rs` y el dibujo Win32 en `src/popup.rs`.

## Plan of Work

Se incorporó `scripts/utils/zcode_session.py` y `src/zcode_session.rs` para leer el almacén cifrado con Windows CNG. Los proveedores de cuota y sus consumidores muestran resultados independientes e inventario de tarjetas agrupado por tipo/vencimiento. El trabajador Rust existente procesa ambos proveedores con el mismo opt-in y conserva errores independientes. No se emiten tarjetas nuevas ni se hace login/refresh de ZCode.

## Concrete Steps

Python activo conserva su intérprete `.venv` y lanzador VBS. El parche AST revisable y su manifest están en `.cache/python-zai-candidate/`. Pruebas focalizadas en `scripts/utils/test_quota_resets.py` y pruebas Rust internas. Rust nuevo: `ai-usage-monitor-rs/target/release/ai-usage-monitor-rs.exe --auto-redeem-resets`. El binario anterior en `target/validation/release/` queda intacto para rollback; también se respaldó el release anterior.

## Validation and Acceptance

Una respuesta Z.AI `success=false` o sin límites debe fallar con un error seguro. Una respuesta válida debe conservar porcentajes y timestamps. Los resets con detalle deben mostrar `Reset DD/MM HH:MM UTC±HH:MM`; los vencidos/canjeados no deben presentarse como disponibles. `cargo test --lib` y compilación deben pasar. La verificación visual y cualquier reinicio se dejan explícitamente pendientes si no se autorizan.

## Idempotence and Recovery

No alterar credenciales ni hacer push. Conservar cambios ajenos. Respaldar y comprobar hashes antes de aplicar cambios focalizados a la copia activa. Conservar el binario Rust anterior para rollback y verificar ruta/PID/lock antes de reiniciar.

## Artifacts and Notes

Salidas temporales en `.cache/`; snapshot Rust en `.cache/rust-validation-snapshot.json`. Respaldos con hashes comprobados: `.cache/zai-integration-20261027-121503/`, incluyendo fuentes de ambos checkouts y release anterior. Respaldo inicial Z.AI: `.cache/quota-resets-backup/zai.rs`. Memorias de proyecto actualizadas en ambos checkouts. Esquema CLI Codex: `.cache/codex-schema/`.

## Interfaces and Dependencies

Sin dependencias nuevas. Usar datetime/chrono para zona local y urllib/reqwest para GET. Referencias primarias: script oficial `zai-org/zai-coding-plugins`, documentación ZCode Usage Stats y OpenAI App Server (`account/rateLimits/read`, detalle opcional de resets y contador autoritativo).

Actualización 2026-10-27: el usuario precisó canje manual; se incorporó el trabajador de Codex y se dejó explícitamente bloqueado Z.AI por autenticación y contrato de canje pendiente.

Actualización posterior 2026-10-27: el usuario autorizó arreglar ambas versiones. El bloqueo anterior se resolvió utilizando la sesión ZCode verificada, se integró el lector nativo en ambas implementaciones y se aplicó el parche a los widgets activos con rollback conservado.
