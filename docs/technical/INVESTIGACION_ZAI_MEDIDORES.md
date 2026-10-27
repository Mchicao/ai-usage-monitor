# Investigación: cuota Z.AI Coding Plan y tarjetas de reset mediante ZCode

Fecha de consulta: 27 de octubre de 2026. La investigación de medidores y contratos se realizó sobre fuentes públicas primarias, sin solicitudes autenticadas. El agente principal complementó ese análisis con la verificación local de solo lectura descrita al final; no hubo canjes ni modificaciones de credenciales o widgets.

## Conclusión operativa

**Cuota del plan y tarjetas de reset requieren consultas y credenciales diferentes.** La cuota se consulta en `/api/monitor/usage/quota/limit`; las tarjetas disponibles se leen en `GET https://zcode.z.ai/api/v1/coding-plan/reset/status`. El segundo endpoint requiere el JWT de ZCode y el token de sesión de la plataforma de la familia activa. Una API key del plan no sustituye esa pareja. Esto está implementado tanto en el colector externo como en el proveedor oficial de ZCode [S1, S4].

Hay una diferencia importante entre versiones: el lector de Token Monitor recupera claves persistidas por ZCode 3.12.3/3.14.0; ZCode público 3.16.2 utiliza **Project Access Tokens temporales**, que declara no persistir en `credentials.json`. No se debe interpretar una clave histórica encontrada en ese archivo como la credencial actual de una instalación nueva [S2, S7, S8].

Recomendación para el padre: mantener independientes la cuota y el inventario de tarjetas; seleccionar familia, identidad y ámbito antes de elegir cualquier credencial; descifrar únicamente entradas conocidas. Para un medidor de solo lectura, limitar las tarjetas a `/reset/status`. Si falta una credencial válida, mostrar información no disponible, sin canjear, emitir tarjetas, crear claves ni obtener tokens nuevos como efecto secundario.

## Fuentes y revisiones fijadas

Se priorizaron dos medidores y la implementación oficial. Los commits se resolvieron mediante la API pública de GitHub y los archivos se leyeron desde `raw.githubusercontent.com`, sin autenticación.

| Fuente | Revisión consultada | Archivos centrales |
| --- | --- | --- |
| Javis603/token-monitor | `60344c5334e7f1e5b35bd897dd94380f20b3306e`, versión `0.80.0`; commit del 27-10-2026 | `src/shared/providers/zai/limits.js`, `zcodeDiscovery.js`, `docs/providers/zai.md`, `tests/shared/zcodeLimits.test.js` |
| PR #725 del mismo proyecto | Integrada el 20-09-2026; merge `8bae8b5c6a0411fa610ade73cf35dcbdd7d6b654`; head `1789a9d5d445905320fe8128230810ffd10f35ea` | Introduce lectura del almacén vivo y selección de claves por identidad [S3] |
| Suppressor72/subscription-monitor | `862e15be714ddff0852163c15c06e8c17c175094`, commit del 22-08-2026 | `collectors/zai.py` [S1] |
| zai-org/ZCode | `3c5b227649371507ddc4eb46ac5133afe6db1cab`, versión declarada `3.16.2`; commit del 20-10-2026 | Proveedor de cuota/reset, cifrado, OAuth y tokens de proyecto [S4–S11] |

El archivo que suele describirse como `zaiLimits.js` corresponde actualmente a `src/shared/providers/zai/limits.js`; la prueba conserva el nombre `tests/shared/zaiLimits.test.js`. La lista pública de archivos de la PR #725 confirma las rutas de proveedor citadas [S3].

## 1. Token Monitor: cuota, billing y selección por identidad

`limits.js` usa `GET /api/monitor/usage/quota/limit` y enriquece las ventanas utilizables con `GET /api/biz/subscription/list`. Sus bases regionales son `https://api.z.ai` y `https://open.bigmodel.cn`. Envía `Authorization: Bearer <key>` y reconoce ventanas de sesión, semanales y MCP; también contempla `CREDIT_LIMIT`, por lo que copiar una clasificación antigua basada solo en `TOKENS_LIMIT` sería insuficiente [S5].

La consulta independiente `https://zcode.z.ai/api/v1/zcode-plan/billing/balance` representa bolsas Start/Weekend. Usa `zcodejwttoken` y `X-Device-Mid`, obtenido por ese proyecto de `telemetry-state.json`. **Billing no es el inventario de tarjetas de reset**: el proveedor revisado no contiene llamadas a `/coding-plan/reset/status`, `/use` ni `/opportunity` [S5, S6].

La PR #725 explica el fallo original: una instalación reciente podía no tener espejo `options.apiKey`; tras cambiar de cuenta, el espejo podía conservar la clave anterior. Su lector reconstruye una entrada concreta desde familia, modalidad e identidad, en lugar de enumerar claves hasta encontrar alguna utilizable [S3].

En `zcodeDiscovery.js` actual:

- Resuelve el directorio base con `ZCODE_DATA_BASE_DIR`, después `HOME`, después `os.homedir()`, agregando `.zcode/v2`.
- Para la generación 3.12.3 lee `providerFamilyConnectionSelections[family].kind`: `start-plan`, `individual-coding-plan` o `team-coding-plan`.
- Descifra `oauth:<family>:user_info`. Acepta un perfil normalizado con `id`, `username` y `displayName` de tipo cadena; para un documento Z.ai crudo usa `user_id`. No busca una identidad en campos alternativos arbitrarios.
- Reconstruye la clave histórica `account-provider:coding-plan:account:<family>-<kind>:account:<encodeURIComponent(identity)>:api-key`.
- Si la identidad es conocida pero esa entrada falta, no envía la consulta de cuota; billing puede seguir con su credencial independiente [S2].

**Objeción al copiarlo literalmente:** todavía conserva fallback al espejo cuando no logra establecer identidad o cuando la entrada de cuota existe pero no se descifra. Ese mecanismo puede aceptar datos antiguos. El código actual restringe además el espejo de billing cuando conoce la identidad, una mejora posterior al alcance original de #725. Sus pruebas cubren explícitamente cambio de cuenta, entrada ausente, perfil BigModel, fallo de descifrado y rechazo del espejo de billing [S2, S12].

El propio proyecto advierte que resolver una clave de equipo no demuestra soporte de cuota de equipo: esa ruta de autodescubrimiento sigue enviando una solicitud personal, sin `type=2` ni encabezados de organización/proyecto. Su documentación también limita la validación de plataforma de ese lector a macOS [S6].

## 2. Subscription Monitor: ejemplo concreto de tarjetas en solo lectura

`collectors/zai.py` separa dos flujos: cuota con `GLM_API_KEY`/`ZAI_API_KEY` y tarjetas con las entradas `zcodejwttoken` y `oauth:zai:access_token` del almacén ZCode. Descifra `enc:v1:` con `cryptography.AESGCM` y consulta `/reset/status`; convierte `available_five_hour_resets` y `available_week_resets` en elementos con vencimiento. Los errores de tarjetas no impiden obtener cuota. No implementa canje [S1].

Sus límites son relevantes: elige Z.ai y, si falta, BigModel sin comprobar familia activa; permite ámbito de equipo por variables de entorno; su resolución de rutas usa overrides propios `ZCODE_CREDENTIALS`/`ZCODE_HOME`, distintos de la cadena oficial. Su clasificación trata `TOKENS_LIMIT` como 5 horas y no distingue correctamente todas las ventanas actuales. Es una referencia útil para el GET de tarjetas, no un contrato suficiente de identidad, rutas o cuotas modernas [S1].

## 3. ZCode oficial: credenciales distintas y evolución actual

### Qué representa cada valor

| Valor | Origen y uso demostrado | No confundir con |
| --- | --- | --- |
| API key manual del usuario | Material explícitamente configurado para consultas compatibles con la plataforma; Token Monitor lo usa para cuota y suscripción [S5] | JWT de login o una referencia de configuración |
| Clave de proyecto histórica | Entrada `account-provider:…:api-key` recuperada por #725; pertenece a un ámbito de cuenta concreto [S2, S3] | Credencial actual garantizada en ZCode 3.16.2 |
| `oauth:zai:access_token` en Desktop actual | **JWT de negocio/plataforma** devuelto por `/api/auth/z/login`; se persiste después del intercambio OAuth [S9] | El OAuth token original de `data.zai.access_token`, el JWT ZCode o un PAT |
| `oauth:bigmodel:access_token` | Sesión de plataforma BigModel; se selecciona exclusivamente cuando el ámbito pertenece a esa familia [S4] | Fallback universal para cuentas Z.ai |
| `zcodejwttoken` | JWT del backend ZCode, separado del anterior; se guarda desde `OAuthTokenSet.zcodeJwtToken` [S9, S10] | API key de cuota o JWT de plataforma |
| Project Access Token, PAT | Token temporal emitido por la plataforma para la cuenta/proyecto; el proveedor oficial actual lo recibe como `auth.apiKey` y lo usa para cuota [S4, S7, S8] | Un API key ID, una clave duradera o el JWT de login |
| `apiKeyId`, provider IDs y referencias | Identificadores para localizar un recurso o resolver material; por sí solos no son secretos de autenticación [S8, S11] | El contenido que debe ir en `Authorization` |

El nombre `access_token` no basta para deducir el tipo de token. `zaiProviderAdapter.ts` distingue `data.token` —JWT ZCode— de `data.zai.access_token` —OAuth original— y convierte el segundo en JWT de negocio. El flujo de polling también se normaliza antes de persistir. El documento oficial distingue asimismo Desktop de Standalone, donde el token original puede almacenarse y el intercambio ocurre en otro punto [S7, S9].

### Identidad y ámbito

`oauthCredentialRepo.ts` guarda `oauth:active_provider`, el token por familia y `oauth:<family>:user_info`; preserva el documento crudo de Z.ai cuando existe. Ese archivo confirma la transformación `user_id` → perfil normalizado, utilizada como evidencia por el lector externo [S10].

`accountProviderCredentialKey.ts` actual conserva un constructor explícito: para Individual combina proveedor e identidad codificada; para Team incluye además producto, organización y proyecto. **No es idéntico al patrón histórico de #725.** El servicio de autorización actual exige que la cuenta siga vigente; no ofrece fallback al Registry [S11].

Decisión recomendada: no convertir `account:…`, nombres de entradas, `project-key-location:v1:…`, IDs de claves ni espejos `options.apiKey` en credenciales de sesión por su mera presencia. Si no se puede demostrar la procedencia y el ámbito de un valor, no utilizarlo para atribuir cuota a una identidad.

### Cambio a PAT y límite de la lectura pasiva

En 3.16.2, `accountProviderCredentialService.ts` declara que el método llamado `loadCodingPlanApiKey` devuelve material de solicitud y no lee/escribe el caché de claves duraderas. La documentación prescribe no depender de las antiguas entradas `account-provider:*:api-key`; los PAT no se escriben en configuración, sesiones, UI, logs ni `credentials.json` [S7].

El contrato de emisión está en `project-access-token-client.ts`: `POST /api/biz/v1/organization/{org}/projects/{project}/api_keys/{id}/access_tokens`, con JWT de login en `Authorization: Bearer …`, `clientType=zcode` y la versión real del cliente. La localización previa puede consultar o **crear** una clave dedicada; solo los IDs de ubicación se persisten, separados por origen, familia, identidad y proyecto [S7, S8].

Esto se documenta únicamente como explicación del cambio: **no se ejecutó ni se recomienda incorporarlo a escondidas en un lector pasivo**. Si el padre encuentra una versión de esta generación, un lector de `credentials.json` no puede prometer recuperar el PAT actual. Una API key manual ya autorizada es una alternativa para su flujo de cuota, pero no demuestra que corresponda a la misma cuenta que las tarjetas.

## 4. Cifrado `enc:v1:` verificado en la fuente oficial

`credentialCipherProvider.ts` define [S13]:

```text
enc:v1:<base64url(iv)>.<base64url(authTag)>.<base64url(ciphertext)>
algoritmo = AES-256-GCM
iv = 12 bytes; authTag = 16 bytes
key = SHA-256(UTF-8(secret))
secret = ZCODE_CREDENTIAL_SECRET, si está configurado;
         en otro caso:
         zcode-credential-fallback:<os.platform()>:<os.homedir()>:<os.userInfo().username>
```

Si la consulta del usuario del sistema falla, la fuente usa `unknown`. La derivación usa el home del sistema, no necesariamente el directorio redirigido de datos. En Windows, `os.platform()` aporta `win32`; reconstruir el secreto exige reproducir exactamente los valores usados por Node.

El descifrado autentica el tag; un tag inválido o una clave incorrecta son fallos, no texto alternativo. El código exige tres segmentos y permite ciphertext vacío válido. En Python, la adaptación observada entrega `ciphertext + tag` a `AESGCM.decrypt`, sin AAD [S1, S13].

La implementación oficial devuelve valores sin el prefijo `enc:v1:` tal cual para el formato anterior; eso **no valida que cualquier cadena sea una credencial**. Una versión `enc:` desconocida no debe enviarse como token. Tampoco se deben registrar valores descifrados ni usar fallos de descifrado como motivo para seleccionar otra identidad.

## 5. Contrato de tarjetas: lectura y mutaciones, solo por fuente

La base predeterminada oficial es `https://zcode.z.ai`; el constructor admite orígenes de entorno. Para Z.ai, la cuota oficial se construye sobre `https://api.z.ai`; el BigModel oficial actual usa por defecto `https://bigmodel.cn`, distinto de `open.bigmodel.cn` en Token Monitor [S4, S14]. No se debe trasladar una credencial entre hosts por analogía.

El método `resolveCodingPlanResetAuthorization` exige ámbito de cuenta, verifica su vigencia, lee `zcodejwttoken` y selecciona **exactamente** `oauth:zai:access_token` o `oauth:bigmodel:access_token`. El comentario prohíbe fallback entre familias [S4, líneas 649–677].

Encabezados de `/status`, `/opportunity` y `/use`:

```text
Authorization: Bearer <zcodejwttoken>
X-Bigmodel-Authorization: <JWT de plataforma de la familia activa>
Bigmodel-Target-Type: PERSONAL | TEAM
Bigmodel-Organization: <organizationId>   # solo TEAM
Bigmodel-Project: <projectId>             # solo TEAM
```

El JWT de plataforma se envía sin agregar `Bearer`. El constructor específico de reset no añade `X-Device-Mid`; ese requisito observado en billing no se debe extrapolar aquí [S4, líneas 982–998; S6].

| Método y ruta bajo `/api/v1/coding-plan/reset` | Contrato observado | Efecto |
| --- | --- | --- |
| `GET /status` | Sin body; timeout de 15 s; envelope `{code,msg?,data?}`, éxito `code:0` | Lee tarjetas disponibles e historial [S4] |
| `POST /opportunity` | JSON `{idempotency_key}`; clave no vacía tras trim, máximo 64 caracteres | Puede emitir una tarjeta. `code:0` devuelve `{granted:true}`; `code:3301`, `{granted:false,next_try_at}`. HTTP 429 tiene tratamiento propio [S4] |
| `POST /use` | JSON `{idempotency_key,reset_type}`; `reset_type` es `FIVE_HOUR` o `WEEK`; misma validación de clave | Consume una oportunidad y resetea esa cuota; éxito `{used:true}` [S4, S15] |
| `POST /history/read` | Sin body en este cliente; mismos dos tokens, sin encabezados de target | Modifica el cursor compartido de historial leído [S4] |

Ninguno de los POST es necesario para mostrar el inventario. No se ejecutó ningún endpoint de esta tabla.

`GET /status` valida este contenido de `data` [S4, líneas 86–113]:

```json
{
  "available_five_hour_resets": [{ "expire_at": 1234567890000 }],
  "available_week_resets": [{ "expire_at": 1234567890000 }],
  "latest_five_hour_reset_history": { "used_at": 1234567890000 },
  "latest_week_reset_history": null,
  "has_unread_history": false
}
```

Los números son marcadores ilustrativos, no datos de una cuenta. Los timestamps son Unix en milisegundos [S16]. Cada lista permite obtener el número disponible por tipo y su vencimiento. Los historiales pueden ser `null`; el esquema no contiene un ID de tarjeta ni un contador de todos los resets recibidos. Por tanto, **no permite deducir ese acumulado de vida** solo a partir de este contrato. `/use` elige tipo de reset, no un `card_id` [S4, S15].

La documentación pública de Usage Stats confirma soporte desde 3.8.1, necesidad de login y Coding Plan de esa cuenta, vencimiento de tarjetas y emisión fuera de hora punta condicionada por uso y límite diario. Los horarios y umbrales son dinámicos: no deben fijarse desde una captura o extrapolarse del horario de idle-time tasks. Usar únicamente una API key no habilita la emisión de tarjetas. La UI puede pedir oportunidades automáticamente; reproducir ese comportamiento en un monitor dejaría de ser solo lectura [S17, S4].

## 6. Mecanismo recomendado y objeciones pendientes

1. **Separar resultados y errores.** La consulta de cuota y la de tarjetas deben conservar éxito/fallo independientes. Un error de tarjetas significa desconocido, no cero. No fusionar identidades solo porque ambas credenciales estén en el mismo equipo.
2. **Resolver versión, familia y cuenta antes del secreto.** Para una generación histórica compatible, reconstruir la entrada exacta; para PAT actuales, no usar entradas antiguas como sustituto. El padre debe contrastar su instalación, sin asumir que el esquema 3.12.3 sigue vigente.
3. **Preferir el contrato oficial para tarjetas.** Dos tokens de la misma familia/sesión y `GET /status`, con ámbito personal o de equipo demostrado. No copiar el fallback Z.ai → BigModel del colector externo.
4. **No usar espejos ni referencias como prueba de autenticidad.** El fallback externo existe y está documentado, pero no satisface una atribución estricta por identidad. Ante entrada ausente, versión desconocida o fallo de autenticación, conservar el estado no disponible.
5. **Mantener todo el flujo pasivo.** No llamar a `/opportunity`, `/use`, `/history/read`, login, refresh, creación/copia de API keys ni emisión de PAT. Un futuro canje requeriría una acción separada y explícita; el contrato citado ya permite revisarlo sin ejecutarlo.

Límites de la evidencia: se verificaron fuentes y contratos del cliente, no respuestas reales ni derechos de ninguna cuenta. Las pruebas de terceros se inspeccionaron, pero no se ejecutaron aquí. La documentación oficial de PAT declara que varios endpoints de cuota tienen regresiones automáticas y que no se ejecutó su comprobación contra el backend real; no se presenta ese soporte como una prueba de producción [S7]. No se comprobó Windows con credenciales reales. No se revisaron datos locales ni se modificaron procesos, credenciales o configuración.

## 7. Verificación local de solo lectura: ZCode 3.16.3 en este equipo

El agente principal comprobó la instalación abierta en `C:\Program Files\ZCode`, versión 3.16.3, y el código de cifrado y proveedor de uso de su `resources/app.asar`. Se leyó `~/.zcode/v2/credentials.json` y se descifraron las entradas necesarias exclusivamente en memoria mediante Node crypto, reproduciendo el algoritmo del cliente instalado. No se imprimieron, exportaron ni modificaron secretos.

La clave histórica del plan, localizada por el `user_id` del perfil Z.ai, fue rechazada por `/api/monitor/usage/quota/limit` con `success=false` y código 1000. En cambio, el **JWT de plataforma** almacenado como `oauth:zai:access_token` devolvió HTTP 200, `success=true` y código 200, tanto con autorización raw como Bearer. El nombre de la entrada no significa que sea el OAuth original: corresponde a la distinción explicada en S9.

La respuesta real identificó plan Lite y dos bolsas: `TOKENS_LIMIT` con 0 % usado (100 % restante, unit=3/number=5) y `TIME_LIMIT` con 29 % usado (71 % restante, 29/100, unit=5/number=1). No se devolvió `nextResetTime` para la ventana de modelos; no se debe inventar su hora de reinicio ni deducir una ventana semanal ausente.

El GET de `/api/v1/coding-plan/reset/status`, usando `zcodejwttoken` y el JWT de plataforma con los encabezados oficiales y ámbito PERSONAL, devolvió código 0: siete tarjetas de cinco horas y ninguna semanal. Cuatro vencen el **28/10/2026 a las 09:59:00 UTC-03:00**; tres, el **22/12/2026 a las 12:59:59 UTC-03:00**.

Esta prueba corrige el diagnóstico anterior: **la sesión disponible sí permite leer cuota y tarjetas; no era necesario pedir otra API key para este equipo**. La cautela sobre PAT sigue siendo válida para no reutilizar claves históricas como si fueran actuales, pero no invalida el GET satisfactorio con el JWT de plataforma.

La integración recomendada para este checkout es un lector de sesión por familia/identidad, con cuota y tarjetas como resultados independientes y errores explícitos. No se requiere emitir un PAT para repetir la consulta que ya funcionó aquí. No se ejecutó `/use`, `/opportunity`, `/history/read`, login, refresh ni creación de claves. La integración en los widgets y el canje automático Z.AI siguen pendientes; este informe no acredita su implementación.

Evidencia local resumida, sin secretos: `.cache/zcode-investigation/local-summary.json`. Las cifras anteriores corresponden al momento de la comprobación del 27/10/2026 y pueden cambiar con el uso.

## Referencias primarias

- **S1.** [Suppressor72: `collectors/zai.py`, commit fijado](https://github.com/Suppressor72/subscription-monitor/blob/862e15be714ddff0852163c15c06e8c17c175094/collectors/zai.py).
- **S2.** [Token Monitor: `src/shared/providers/zai/zcodeDiscovery.js`](https://github.com/Javis603/token-monitor/blob/60344c5334e7f1e5b35bd897dd94380f20b3306e/src/shared/providers/zai/zcodeDiscovery.js), especialmente líneas 89–207 y 274–341.
- **S3.** [Token Monitor: PR #725, descripción y alcance](https://github.com/Javis603/token-monitor/pull/725); [metadatos públicos](https://api.github.com/repos/Javis603/token-monitor/pulls/725); [archivos cambiados](https://api.github.com/repos/Javis603/token-monitor/pulls/725/files?per_page=100).
- **S4.** [ZCode: `packages/services/src/usage-stats/providers/bigmodelUsageQuotaProvider.ts`](https://github.com/zai-org/ZCode/blob/3c5b227649371507ddc4eb46ac5133afe6db1cab/packages/services/src/usage-stats/providers/bigmodelUsageQuotaProvider.ts), líneas 73–113, 523–677, 834–886 y 982–1099.
- **S5.** [Token Monitor: `src/shared/providers/zai/limits.js`](https://github.com/Javis603/token-monitor/blob/60344c5334e7f1e5b35bd897dd94380f20b3306e/src/shared/providers/zai/limits.js), rutas en líneas 20–31 y billing en 649–670.
- **S6.** [Token Monitor: `docs/providers/zai.md`](https://github.com/Javis603/token-monitor/blob/60344c5334e7f1e5b35bd897dd94380f20b3306e/docs/providers/zai.md), límites de credenciales, equipos y plataformas en líneas 31–51.
- **S7.** [ZCode: `docs/model-provider-project-access-token.md`](https://github.com/zai-org/ZCode/blob/3c5b227649371507ddc4eb46ac5133afe6db1cab/docs/model-provider-project-access-token.md), líneas 9–50, 74–78 y 118–140; [servicio `accountProviderCredentialService.ts`](https://github.com/zai-org/ZCode/blob/3c5b227649371507ddc4eb46ac5133afe6db1cab/packages/services/src/model-provider/accountProviderCredentialService.ts).
- **S8.** [ZCode: `packages/shared/src/project-access-token-client.ts`](https://github.com/zai-org/ZCode/blob/3c5b227649371507ddc4eb46ac5133afe6db1cab/packages/shared/src/project-access-token-client.ts), ubicación aislada en línea 141, emisión en 204–230 y localización/creación en 252–353.
- **S9.** [ZCode: `packages/services/src/oauth/providers/zaiProviderAdapter.ts`](https://github.com/zai-org/ZCode/blob/3c5b227649371507ddc4eb46ac5133afe6db1cab/packages/services/src/oauth/providers/zaiProviderAdapter.ts), líneas 298–305 y 355–388; [intercambio `zaiBusinessTokenResolver.ts`](https://github.com/zai-org/ZCode/blob/3c5b227649371507ddc4eb46ac5133afe6db1cab/packages/services/src/providers/zaiBusinessTokenResolver.ts).
- **S10.** [ZCode: `packages/services/src/oauth/repo/oauthCredentialRepo.ts`](https://github.com/zai-org/ZCode/blob/3c5b227649371507ddc4eb46ac5133afe6db1cab/packages/services/src/oauth/repo/oauthCredentialRepo.ts), claves y perfiles en líneas 13–33, 350–354 y 365–425.
- **S11.** [ZCode: `accountProviderCredentialKey.ts`](https://github.com/zai-org/ZCode/blob/3c5b227649371507ddc4eb46ac5133afe6db1cab/packages/services/src/model-provider/accountProviderCredentialKey.ts); [frontera `accountRequestAuthService.ts`](https://github.com/zai-org/ZCode/blob/3c5b227649371507ddc4eb46ac5133afe6db1cab/packages/services/src/model-provider/accountRequestAuthService.ts).
- **S12.** [Token Monitor: `tests/shared/zcodeLimits.test.js`](https://github.com/Javis603/token-monitor/blob/60344c5334e7f1e5b35bd897dd94380f20b3306e/tests/shared/zcodeLimits.test.js), casos de almacén/identidad en líneas 596–967.
- **S13.** [ZCode: `credentialCipherProvider.ts`](https://github.com/zai-org/ZCode/blob/3c5b227649371507ddc4eb46ac5133afe6db1cab/packages/services/src/credential/providers/credentialCipherProvider.ts); [lectura de `credentials.json`](https://github.com/zai-org/ZCode/blob/3c5b227649371507ddc4eb46ac5133afe6db1cab/packages/services/src/credential/credentialService.ts); [resolución de directorios](https://github.com/zai-org/ZCode/blob/3c5b227649371507ddc4eb46ac5133afe6db1cab/packages/services/src/paths.ts).
- **S14.** [ZCode: `packages/shared/src/zcodeEndpoint.ts`](https://github.com/zai-org/ZCode/blob/3c5b227649371507ddc4eb46ac5133afe6db1cab/packages/shared/src/zcodeEndpoint.ts), orígenes en líneas 3–11 y builders en 204–268 y 341–384.
- **S15.** [ZCode: `packages/shared/src/coding-plan-reset.ts`](https://github.com/zai-org/ZCode/blob/3c5b227649371507ddc4eb46ac5133afe6db1cab/packages/shared/src/coding-plan-reset.ts).
- **S16.** [ZCode: `docs/coding-plan-quota-reset-review.md`](https://github.com/zai-org/ZCode/blob/3c5b227649371507ddc4eb46ac5133afe6db1cab/docs/coding-plan-quota-reset-review.md), contrato y corrección de JWT en líneas 14–34; registro histórico de integración, subordinado al código actual.
- **S17.** [Documentación pública oficial: Usage Stats → Quota Reset Cards](https://www.zcode.network/en/docs/usage-stats/#quota-reset-cards), consultada el 27-10-2026.

Versiones verificadas en [`package.json` de Token Monitor](https://github.com/Javis603/token-monitor/blob/60344c5334e7f1e5b35bd897dd94380f20b3306e/package.json) y [`package.json` de ZCode](https://github.com/zai-org/ZCode/blob/3c5b227649371507ddc4eb46ac5133afe6db1cab/package.json). Los enlaces fijados evitan atribuir cambios posteriores a esta investigación.
