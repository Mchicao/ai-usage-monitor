use std::{env, path::PathBuf, process::Stdio, sync::atomic::{AtomicBool, AtomicU8, Ordering}, time::Duration};

use chrono::{DateTime, Utc};
use serde_json::{json, Value};
use tokio::{io::{AsyncBufReadExt, AsyncWriteExt, BufReader}, process::Command};

use crate::models::{AllQuotasSnapshot, ResetCredit, ResetCredits};

pub static ENABLED: AtomicBool = AtomicBool::new(false);
static LAST_RESULT: AtomicU8 = AtomicU8::new(0);
const LEAD_SECONDS: i64 = 600;

pub fn display_status() -> &'static str {
    if !ENABLED.load(Ordering::Relaxed) { return "Canje automático desactivado"; }
    match LAST_RESULT.load(Ordering::Relaxed) {
        1 => "Canje auto: último reset aplicado",
        2 => "Canje auto: error; revisa el registro",
        _ => "Canje auto: 10 min antes",
    }
}

fn due_credit(credits: &ResetCredits, now: i64) -> Option<&ResetCredit> {
    if credits.available_count.unwrap_or(0) <= 0 { return None; }
    credits.credits.iter().filter_map(|credit| {
        let expiry = DateTime::parse_from_rfc3339(credit.expires_at.as_deref()?).ok()?.timestamp();
        let remaining = expiry.checked_sub(now)?;
        (credit.id.as_deref().is_some_and(|id| !id.is_empty()) && (1..=LEAD_SECONDS).contains(&remaining))
            .then_some((expiry, credit))
    }).min_by_key(|(expiry, _)| *expiry).map(|(_, credit)| credit)
}

fn codex_binary() -> PathBuf {
    if let Some(path) = env::var_os("AI_CODEX_BINARY") { return path.into(); }
    if let Some(appdata) = env::var_os("APPDATA") {
        let (package, target) = if cfg!(target_arch = "aarch64") {
            ("codex-win32-arm64", "aarch64-pc-windows-msvc")
        } else { ("codex-win32-x64", "x86_64-pc-windows-msvc") };
        let candidate = PathBuf::from(appdata).join("npm/node_modules/@openai/codex/node_modules/@openai")
            .join(package).join("vendor").join(target).join("bin/codex.exe");
        if candidate.is_file() { return candidate; }
    }
    "codex.exe".into()
}

async fn rpc(
    input: &mut tokio::process::ChildStdin,
    output: &mut tokio::io::Lines<BufReader<tokio::process::ChildStdout>>,
    id: u64,
    method: &str,
    params: Value,
) -> Result<Value, String> {
    let request = json!({"id": id, "method": method, "params": params});
    input.write_all(format!("{}\n", request).as_bytes()).await.map_err(|_| "No se pudo enviar la operación a Codex.")?;
    tokio::time::timeout(Duration::from_secs(20), async {
        loop {
            let line = output.next_line().await.map_err(|_| "No se pudo leer la respuesta de Codex.")?
                .ok_or("Codex cerró la conexión.")?;
            let response: Value = serde_json::from_str(&line).map_err(|_| "Respuesta de Codex no válida.")?;
            if response.get("id").and_then(Value::as_u64) != Some(id) { continue; }
            if response.get("error").is_some() { return Err("Codex rechazó la operación de resets.".to_owned()); }
            return response.get("result").cloned().ok_or_else(|| "Codex no devolvió resultado.".to_owned());
        }
    }).await.map_err(|_| "Codex no respondió dentro de 20 segundos.".to_owned())?
}

// Solo este proceso Rust canjea: Python continúa como lector de cuotas.
async fn reset_operation(credit_id: Option<&str>) -> Result<Value, String> {
    let mut command = Command::new(codex_binary());
    command.arg("app-server").stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::null()).kill_on_drop(true);
    #[cfg(windows)]
    command.creation_flags(0x08000000);
    let mut child = command.spawn().map_err(|_| "No se pudo iniciar Codex. Revisa AI_CODEX_BINARY.".to_owned())?;
    let mut input = child.stdin.take().ok_or("Codex no abrió su entrada.")?;
    let mut output = BufReader::new(child.stdout.take().ok_or("Codex no abrió su salida.")?).lines();
    rpc(&mut input, &mut output, 1, "initialize", json!({
        "clientInfo": {"name": "ai_usage_monitor", "version": "0.1.0"},
        "capabilities": {"experimentalApi": true}
    })).await?;
    input.write_all(b"{\"method\":\"initialized\"}\n").await.map_err(|_| "No se pudo inicializar Codex.")?;
    let limits = rpc(&mut input, &mut output, 2, "account/rateLimits/read", Value::Null).await?;
    let Some(credit_id) = credit_id else { return Ok(limits); };
    // Confirmar de nuevo ID, estado y vencimiento en la cuenta actual antes de gastar nada.
    let eligible = limits.pointer("/rateLimitResetCredits/credits").and_then(Value::as_array)
        .into_iter().flatten().any(|credit| {
            credit.get("id").and_then(Value::as_str) == Some(credit_id)
                && credit.get("status").and_then(Value::as_str) == Some("available")
                && credit.get("expiresAt").and_then(Value::as_i64)
                    .and_then(|expiry| expiry.checked_sub(Utc::now().timestamp()))
                    .is_some_and(|remaining| (1..=LEAD_SECONDS).contains(&remaining))
        });
    if !eligible { return Err("El reset ya no está disponible en la cuenta Codex actual.".into()); }
    // La clave estable identifica el mismo canje incluso después de reiniciar o de un timeout.
    let outcome = rpc(&mut input, &mut output, 3, "account/rateLimitResetCredit/consume", json!({
        "creditId": credit_id, "idempotencyKey": format!("ai-usage-monitor:{}", credit_id)
    })).await?;
    match outcome.get("outcome").and_then(Value::as_str) {
        Some("reset" | "alreadyRedeemed") => {
            rpc(&mut input, &mut output, 4, "account/rateLimits/read", Value::Null).await?;
            Ok(outcome)
        }
        Some("nothingToReset") => Err("Codex todavía no permite aplicar este reset; se reintentará antes de vencer.".into()),
        Some("noCredit") => Err("Codex no tiene resets disponibles para canjear.".into()),
        _ => Err("Codex devolvió un resultado de canje desconocido.".into()),
    }
}

pub async fn check_access() -> Result<i64, String> {
    let limits = reset_operation(None).await?;
    limits.pointer("/rateLimitResetCredits/availableCount").and_then(Value::as_i64)
        .ok_or_else(|| "Codex no devolvió información de resets.".into())
}

pub async fn redeem_due(snapshot: &AllQuotasSnapshot) -> Result<bool, String> {
    if !ENABLED.load(Ordering::Relaxed) { return Ok(false); }
    let now = Utc::now().timestamp();
    let credit = snapshot.codex.data.as_ref().into_iter().flat_map(|data| &data.accounts)
        .filter_map(|account| account.reset_credits.as_ref()).filter_map(|credits| due_credit(credits, now))
        .min_by_key(|credit| credit.expires_at.as_deref().and_then(|s| DateTime::parse_from_rfc3339(s).ok()).map(|d| d.timestamp()));
    let Some(credit) = credit else { return Ok(false); };
    if let Err(error) = reset_operation(credit.id.as_deref()).await {
        LAST_RESULT.store(2, Ordering::Relaxed);
        return Err(error);
    }
    LAST_RESULT.store(1, Ordering::Relaxed);
    Ok(true)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn select_earliest_only_in_last_ten_minutes() {
        let credits = ResetCredits { available_count: Some(4), credits: [0, 601, 600, 1].into_iter().map(|seconds| ResetCredit {
            id: Some(format!("credit_{}", seconds)),
            expires_at: Some(DateTime::<Utc>::from_timestamp(1900000000 + seconds, 0).unwrap().to_rfc3339()),
        }).collect() };
        assert_eq!(due_credit(&credits, 1900000000).unwrap().id.as_deref(), Some("credit_1"));
        assert!(due_credit(&credits, 1900000601).is_none());
        assert!(due_credit(&credits, 1899999399).is_none());
    }
}
