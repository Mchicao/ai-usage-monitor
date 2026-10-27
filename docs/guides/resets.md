# Cuotas y resets manuales

Los reinicios de ventana renuevan cuota de forma periódica. Los resets manuales son créditos distintos que pueden caducar. El popup muestra cada vencimiento como `Reset DD/MM HH:MM UTC±HH:MM`, convertido a la zona local de Windows.

## Codex

Rust consulta el detalle de resets, además del contador. El contador es autoritativo: puede haber más créditos que filas de detalle. Si el servicio no informa una hora, se muestra `Hora no disponible`.

El canje automático se activa al iniciar Rust con `--auto-redeem-resets`. El monitor comprueba cada minuto el primer crédito que vence dentro de los próximos diez minutos, incluso si el modo de juego pausa la recolección de cuotas. El PC y el widget deben permanecer encendidos; un equipo suspendido no puede canjear.

Antes de canjear, Rust inicia el Codex instalado mediante su App Server, consulta sus resets y exige que ese mismo ID siga disponible y próximo a vencer. No inicia sesión en otra cuenta. La clave de idempotencia es estable por crédito, para repetir el mismo intento tras un timeout sin gastar otro reset. Se aplica como máximo un crédito por comprobación y después se vuelven a consultar las cuotas. Si Codex devuelve `nothingToReset`, el monitor no declara éxito y vuelve a intentarlo en el siguiente minuto.

El popup indica si el canje está activado. Para desactivarlo, sal del Rust desde la bandeja y vuelve a iniciarlo sin esa opción. Python solo lee cuotas; no compite por el canje. El registro actual de Rust está en `C:\Proyectos\ai-usage-monitor\debug.log`.

El ejecutable nativo Codex se detecta en la instalación npm de Windows o en PATH. Para otra instalación, configura `AI_CODEX_BINARY` con su ruta. `--check-reset-access` verifica inicialización y lectura de resets sin canjear ninguno.

## Z.AI

Python y Rust leen la sesión activa del Coding Plan personal Z.ai en ZCode. El almacén `.zcode/v2/credentials.json` se busca bajo `ZCODE_DATA_BASE_DIR`, `HOME` o el home de Windows, en ese orden. Se confirma familia `zai`, identidad y selección `individual-coding-plan` en `setting.json`; un ámbito diferente se presenta como error en lugar de consultar una cuenta equivocada.

Las entradas `enc:v1:` se descifran en memoria con AES-GCM mediante Windows CNG, sin Node ni dependencias nuevas. Se reproduce la derivación de ZCode con `ZCODE_CREDENTIAL_SECRET` o el usuario/home de Windows. No se copian ni registran secretos. La cuota usa el JWT de plataforma y el inventario de tarjetas usa además el JWT ZCode. Ambos resultados conservan sus errores por separado: tarjetas desconocidas no significan cero tarjetas.

Si ZCode no está instalado, se conservan las fuentes explícitas `ZAI_API_KEY`/`GLM_API_KEY`, `ZAI_API_KEY_FILE`, OpenCode y `ZAI_PLATFORM_TOKEN`/`ZAI_PLATFORM_TOKEN_FILE`. No se mezclan sus tarjetas con otra sesión. HTTP 200 con `success=false` sigue siendo un error.

Los timestamps `nextResetTime` del servicio se conservan para mostrar los reinicios periódicos. No se inventan ventanas ni porcentajes cuando faltan datos.

Ambos popups muestran el inventario de tarjetas y sus vencimientos, agrupando las que comparten tipo y fecha/hora. Las ventanas semanales `CREDIT_LIMIT` se distinguen de las de cinco horas cuando el servicio las expone.

Con `--auto-redeem-resets`, el trabajador Rust comprueba también Z.AI cada minuto. Solo intenta canjear si existe una tarjeta que vence dentro de diez minutos y la cuota de ese tipo tiene uso; con cuota llena o desconocida no gasta tarjetas. Relee sesión, identidad, cuota e inventario inmediatamente antes del canje. Cada intento usa una clave idempotente estable basada en tipo, vencimiento y cantidad de tarjetas equivalentes. Tras un canje confirmado actualiza las cuotas. Python solo lee y presenta los datos para evitar competir con Rust.

El servicio Z.AI recibe `reset_type` (`FIVE_HOUR` o `WEEK`) y elige la tarjeta: su API no permite seleccionar un ID individual. El monitor no emite nuevas oportunidades ni marca historiales como leídos. Para desactivar el canje de ambos proveedores, inicia Rust sin `--auto-redeem-resets`.

## Verificación

Pruebas Python: `python scripts/utils/test_quota_resets.py` y `python -m unittest test_codex_multi_account`. Rust: `cargo test --manifest-path ai-usage-monitor-rs/Cargo.toml --lib --locked`. Cubren el cifrado autenticado con una credencial ficticia, fallos independientes de cuota/tarjetas y límites temporales del canje. No canjean resets reales. La opción de acceso Codex solo hace lecturas; las pruebas no validan un canje real en los servidores.

Referencias: [Codex App Server](https://learn.chatgpt.com/docs/app-server), [ZCode Usage Stats](https://zcode.z.ai/en/docs/usage-stats) y [consulta oficial de uso Z.AI](https://github.com/zai-org/zai-coding-plugins/blob/main/plugins/glm-plan-usage/skills/usage-query-skill/scripts/query-usage.mjs).
