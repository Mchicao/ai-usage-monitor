use std::{env, fs, path::PathBuf, process::Command};
use chrono::{DateTime, Utc};
use serde_json::Value;
use crate::models::{format_countdown_unix, ServiceResult, ZaiData, ZaiLimit, ZaiReset, ZaiResets};
use crate::zcode_session::{read_session, ZCodeSession};
use std::sync::atomic::{AtomicU8, Ordering};

static AUTO_STATUS: AtomicU8 = AtomicU8::new(0);

pub fn auto_reset_status() -> &'static str {
    match AUTO_STATUS.load(Ordering::Relaxed) {
        1 => "Canje auto Z.AI: último reset aplicado",
        2 => "Canje auto Z.AI: error; revisa el registro",
        _ => "Canje auto Z.AI: 10 min antes",
    }
}

fn api_key() -> Option<String> {
    for name in ["ZAI_API_KEY", "GLM_API_KEY"] {
        if let Ok(key) = env::var(name) {
            if !key.trim().is_empty() { return Some(key.trim().to_owned()); }
        }
    }
    let home = PathBuf::from(env::var("USERPROFILE").ok()?);
    let path = env::var("ZAI_API_KEY_FILE").map(PathBuf::from)
        .unwrap_or_else(|_| home.join(".local/share/opencode/auth.json"));
    if let Ok(content) = fs::read_to_string(path) {
        if let Ok(body) = serde_json::from_str::<Value>(&content) {
            if let Some(key) = body.pointer("/zai-coding-plan/key").and_then(Value::as_str) {
                if !key.trim().is_empty() { return Some(key.trim().to_owned()); }
            }
        }
    }
    let mut command = Command::new("opencode");
    command.args(["auth", "export", "zai-coding-plan"]);
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x08000000);
    }
    let output = command.output().ok()?;
    if !output.status.success() { return None; }
    let body: Value = serde_json::from_slice(&output.stdout).ok()?;
    body.as_array()?.iter().find_map(|entry| entry.pointer("/value/key")
        .and_then(Value::as_str).filter(|key| !key.trim().is_empty()).map(|key| key.trim().to_owned()))
}

fn platform_token() -> Option<String> {
    if let Ok(token) = env::var("ZAI_PLATFORM_TOKEN") {
        if !token.trim().is_empty() { return Some(token.trim().to_owned()); }
    }
    let path = env::var("ZAI_PLATFORM_TOKEN_FILE").map(PathBuf::from).ok().or_else(|| {
        env::var("USERPROFILE").ok().map(|home| PathBuf::from(home).join("AppData/Local/hermes/.zai_platform_token.txt"))
    })?;
    fs::read_to_string(path).ok().map(|token| token.trim().to_owned()).filter(|token| !token.is_empty())
}

fn parse_quota(body: &Value) -> Result<ZaiData, String> {
    if body.get("success").and_then(Value::as_bool) == Some(false) {
        let code = body.get("code").and_then(Value::as_i64).map(|v| v.to_string()).unwrap_or_else(|| "desconocido".into());
        return Err(format!("Z.AI rechazó la consulta (código {}). Revisa tu autenticación.", code));
    }
    let data = body.get("data").ok_or("Z.AI no devolvió límites de Coding Plan.")?;
    let raw_limits = data.get("limits").and_then(Value::as_array)
        .filter(|limits| !limits.is_empty()).ok_or("Z.AI no devolvió límites de Coding Plan.")?;
    let limits = raw_limits.iter().map(|item| {
        let minutes = match item.get("unit").and_then(Value::as_i64) {
            Some(1) => 1440, Some(3) => 60, Some(5) => 1, Some(6) => 10080, _ => 0,
        } * item.get("number").and_then(Value::as_i64).unwrap_or(0);
        let kind = match item.get("type").and_then(Value::as_str) {
            Some("TOKENS_LIMIT" | "CREDIT_LIMIT") if minutes == 10080 => "coding_weekly",
            Some("TOKENS_LIMIT" | "CREDIT_LIMIT") if minutes == 300 => "coding_5h",
            Some("TOKENS_LIMIT") if item.get("unit").is_none() => "coding_5h",
            Some("TIME_LIMIT") => "mcp_tools",
            _ => "other",
        };
        let reset = item.get("nextResetTime").and_then(Value::as_i64).map(|ms| ms / 1000);
        ZaiLimit {
            kind: kind.to_owned(),
            remaining_percent: item.get("remaining").and_then(Value::as_f64)
                .or_else(|| item.get("percentage").and_then(Value::as_f64).map(|used| 100.0 - used)),
            current_prompts: item.get("currentValue").and_then(Value::as_i64),
            max_prompts: item.get("usage").and_then(Value::as_i64),
            reset_at: reset.and_then(|ts| DateTime::<Utc>::from_timestamp(ts, 0)).map(|dt| dt.to_rfc3339()),
            reset_in: reset.map(|ts| format_countdown_unix(Some(ts))),
        }
    }).collect();
    Ok(ZaiData {
        level: data.get("level").and_then(Value::as_str).map(str::to_owned),
        limits,
        email: None,
        reset_credits: ZaiResets::unavailable("Inicia sesión en ZCode para ver tarjetas."),
        account_id: None,
    })
}

fn parse_resets(body: &Value) -> Result<ZaiResets, String> {
    if body.get("code").and_then(Value::as_i64) != Some(0) {
        return Err("Z.AI rechazó la consulta de tarjetas.".into());
    }
    let data = body.get("data").ok_or("Inventario de tarjetas Z.AI no válido.")?;
    let mut credits = Vec::new();
    for (field, kind) in [("available_five_hour_resets", "coding_5h"), ("available_week_resets", "coding_weekly")] {
        let rows = data.get(field).and_then(Value::as_array).ok_or("Inventario de tarjetas Z.AI no válido.")?;
        for row in rows {
            let expiry = row.get("expire_at").and_then(Value::as_i64)
                .filter(|value| *value > 0).and_then(DateTime::<Utc>::from_timestamp_millis)
                .ok_or("Vencimiento de tarjeta Z.AI no válido.")?;
            if expiry > Utc::now() { credits.push(ZaiReset { kind: kind.into(), expires_at: expiry.to_rfc3339() }); }
        }
    }
    credits.sort_by(|a, b| a.expires_at.cmp(&b.expires_at));
    Ok(ZaiResets { status: "ok".into(), credits, error: None })
}

async fn reset_request(client: &reqwest::Client, session: &ZCodeSession, path: &str) -> Result<Value, String> {
    let token = session.zcode_token.as_ref().ok_or("Falta la sesión ZCode para consultar tarjetas.")?;
    let authorization = if token.to_ascii_lowercase().starts_with("bearer ") { token.clone() } else { format!("Bearer {}", token) };
    let response = client.get(format!("https://zcode.z.ai/api/v1/coding-plan/reset/{}", path))
        .header("Authorization", authorization).header("X-Bigmodel-Authorization", &session.platform_token)
        .header("Bigmodel-Target-Type", "PERSONAL").send().await
        .map_err(|_| "No se pudo consultar el inventario de tarjetas Z.AI.")?;
    if !response.status().is_success() { return Err(format!("Tarjetas Z.AI: HTTP {}", response.status().as_u16())); }
    response.json().await.map_err(|_| "Inventario de tarjetas Z.AI no válido.".into())
}

async fn fetch_with_session(client: &reqwest::Client, session: &ZCodeSession) -> ServiceResult<ZaiData> {
    let quota = async {
        let response = client.get("https://api.z.ai/api/monitor/usage/quota/limit")
            .header("Authorization", &session.platform_token).send().await.map_err(|_| "No se pudo consultar la cuota Z.AI.".to_owned())?;
        if !response.status().is_success() { return Err(format!("Cuota Z.AI: HTTP {}", response.status().as_u16())); }
        let body: Value = response.json().await.map_err(|_| "Respuesta Z.AI no válida.")?;
        parse_quota(&body)
    };
    let resets = async { parse_resets(&reset_request(client, &session, "status").await?) };
    let (quota, resets) = tokio::join!(quota, resets);
    let error = quota.as_ref().err().cloned();
    let mut data = quota.unwrap_or_else(|_| ZaiData {
        level: None, limits: Vec::new(), email: None,
        reset_credits: ZaiResets::unavailable("Tarjetas no disponibles"), account_id: None,
    });
    data.email = session.email.clone();
    data.account_id = Some(session.identity.clone());
    data.reset_credits = resets.unwrap_or_else(|error| ZaiResets::unavailable(&error));
    ServiceResult { status: if error.is_some() { "error" } else { "ok" }.into(), data: Some(data), source: Some("ZCode".into()), error }
}

fn due_reset(data: &ZaiData, now: i64) -> Option<&ZaiReset> {
    if data.reset_credits.status != "ok" { return None; }
    data.reset_credits.credits.iter().filter(|credit| {
        let seconds = DateTime::parse_from_rfc3339(&credit.expires_at).ok()
            .and_then(|expiry| expiry.timestamp().checked_sub(now));
        seconds.is_some_and(|remaining| (1..=600).contains(&remaining))
            && data.limits.iter().any(|limit| limit.kind == credit.kind
                && limit.remaining_percent.is_some_and(|value| value.is_finite() && (0.0..100.0).contains(&value)))
    }).min_by_key(|credit| DateTime::parse_from_rfc3339(&credit.expires_at).ok().map(|date| date.timestamp()))
}

async fn redeem_checked(client: &reqwest::Client, previous: &ZaiData) -> Result<bool, String> {
    if due_reset(previous, Utc::now().timestamp()).is_none() { return Ok(false); }
    let session = read_session()?.ok_or("Falta la sesión ZCode para el canje.")?;
    if previous.account_id.as_deref() != Some(session.identity.as_str()) {
        return Err("La cuenta ZCode cambió; se requiere actualizar sus cuotas.".into());
    }
    let fresh = fetch_with_session(client, &session).await;
    if fresh.status != "ok" { return Err("No se pudo revalidar la cuota antes del canje Z.AI.".into()); }
    let data = fresh.data.ok_or("No se pudo revalidar la tarjeta Z.AI.")?;
    if data.reset_credits.status != "ok" { return Err("No se pudo revalidar la tarjeta Z.AI.".into()); }
    let Some(credit) = due_reset(&data, Utc::now().timestamp()) else { return Ok(false); };
    let reset_type = match credit.kind.as_str() {
        "coding_5h" => "FIVE_HOUR",
        "coding_weekly" => "WEEK",
        _ => return Err("Tipo de reset Z.AI no compatible.".into()),
    };
    let expiry = DateTime::parse_from_rfc3339(&credit.expires_at).map_err(|_| "Vencimiento Z.AI no válido.")?.timestamp_millis();
    let count = data.reset_credits.credits.iter().filter(|other| other.kind == credit.kind && other.expires_at == credit.expires_at).count();
    // El servicio elige la tarjeta por tipo: no admite card_id. Nunca consumir con cuota llena.
    let idempotency_key = format!("aum-zai-{}-{}-{}", reset_type, expiry, count);
    let token = session.zcode_token.ok_or("Falta la sesión ZCode para el canje.")?;
    let authorization = if token.to_ascii_lowercase().starts_with("bearer ") { token } else { format!("Bearer {}", token) };
    let response = client.post("https://zcode.z.ai/api/v1/coding-plan/reset/use")
        .header("Authorization", authorization).header("X-Bigmodel-Authorization", session.platform_token)
        .header("Bigmodel-Target-Type", "PERSONAL")
        .json(&serde_json::json!({"idempotency_key": idempotency_key, "reset_type": reset_type}))
        .send().await.map_err(|_| "El canje Z.AI no se confirmó; se reutilizará la misma clave al reintentar.")?;
    if !response.status().is_success() { return Err(format!("Canje Z.AI: HTTP {}", response.status().as_u16())); }
    let body: Value = response.json().await.map_err(|_| "El canje Z.AI no devolvió una respuesta válida.")?;
    if body.get("code").and_then(Value::as_i64) != Some(0) || body.pointer("/data/used").and_then(Value::as_bool) != Some(true) {
        return Err("Z.AI no confirmó el canje de la tarjeta.".into());
    }
    Ok(true)
}

pub async fn redeem_due(client: &reqwest::Client, result: &ServiceResult<ZaiData>) -> Result<bool, String> {
    if result.status != "ok" || result.source.as_deref() != Some("ZCode") { return Ok(false); }
    let Some(data) = result.data.as_ref() else { return Ok(false); };
    let outcome = redeem_checked(client, data).await;
    match &outcome {
        Ok(true) => AUTO_STATUS.store(1, Ordering::Relaxed),
        Err(_) => AUTO_STATUS.store(2, Ordering::Relaxed),
        _ => {}
    }
    outcome
}

pub async fn fetch_zai_quota(client: &reqwest::Client) -> ServiceResult<ZaiData> {
    match read_session() {
        Ok(Some(session)) => return fetch_with_session(client, &session).await,
        Err(error) => return ServiceResult { status: "error".into(), data: None, source: Some("ZCode".into()), error: Some(error) },
        Ok(None) => {}
    }
    let key = api_key();
    let authorization = key.as_ref().cloned().or_else(|| platform_token().map(|token| format!("Bearer {}", token)));
    let result = async {
        let authorization = authorization.ok_or("Falta autenticación de Z.AI. Configura ZAI_API_KEY o inicia sesión en OpenCode.")?;
        let mut request = client.get("https://api.z.ai/api/monitor/usage/quota/limit")
            .header("Authorization", authorization).header("Accept", "application/json");
        if key.is_none() {
            for (header, name) in [("bigmodel-organization", "ZAI_ORG_ID"), ("bigmodel-project", "ZAI_PROJECT_ID")] {
                if let Ok(value) = env::var(name) { request = request.header(header, value); }
            }
        }
        let response = request.send().await.map_err(|_| "No se pudo conectar con Z.AI.".to_owned())?;
        if !response.status().is_success() { return Err(format!("Z.AI respondió HTTP {}.", response.status().as_u16())); }
        let body: Value = response.json().await.map_err(|_| "Respuesta Z.AI no válida.".to_owned())?;
        parse_quota(&body)
    }.await;
    match result {
        Ok(data) => ServiceResult { status: "ok".into(), data: Some(data), source: Some("Z.AI API".into()), error: None },
        Err(error) => ServiceResult { status: "error".into(), data: None, source: Some("Z.AI API".into()), error: Some(error) },
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn reject_business_errors_and_empty_quota() {
        assert!(parse_quota(&json!({"success": false, "code": 1000})).is_err());
        assert!(parse_quota(&json!({"data": {"limits": []}})).is_err());
    }

    #[test]
    fn preserve_reset_time_and_unknown_percent() {
        let data = parse_quota(&json!({"success": true, "data": {"limits": [
            {"type": "TOKENS_LIMIT", "percentage": 25, "nextResetTime": 1900000000000_i64},
            {"type": "TIME_LIMIT"}
        ]}})).unwrap();
        assert_eq!(data.limits[0].remaining_percent, Some(75.0));
        assert_eq!(DateTime::parse_from_rfc3339(data.limits[0].reset_at.as_deref().unwrap()).unwrap().timestamp(), 1900000000);
        assert_eq!(data.limits[1].remaining_percent, None);
    }

    #[test]
    fn cards_group_expiry_and_only_redeem_due_used_quota() {
        let body = json!({"code": 0, "data": {"available_five_hour_resets": [
            {"expire_at": 1900000000000_i64}, {"expire_at": 1900000000000_i64}
        ], "available_week_resets": []}});
        let resets = parse_resets(&body).unwrap();
        assert_eq!(resets.credits.len(), 2);
        assert_eq!(resets.display_lines().len(), 2);
        let mut data = parse_quota(&json!({"data": {"limits": [{"type": "TOKENS_LIMIT", "percentage": 0}]}})).unwrap();
        data.reset_credits = resets;
        assert!(due_reset(&data, 1899999400).is_none());
        data.limits[0].remaining_percent = Some(50.0);
        assert!(due_reset(&data, 1899999400).is_some());
        assert!(due_reset(&data, 1899999399).is_none());
        assert!(due_reset(&data, 1900000000).is_none());
        assert!(parse_resets(&json!({"code": 1000, "data": {}})).is_err());
    }
}
