use std::collections::HashMap;
use std::fs;
use std::path::PathBuf;

use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use base64::Engine;
use chrono::{DateTime, Utc};
use serde_json::Value;

use crate::models::{
    format_countdown_unix, CodexAccount, CodexData, CodexWindow, ServiceResult,
};

fn home_dir() -> Option<PathBuf> {
    std::env::var("USERPROFILE").ok().map(PathBuf::from)
}

fn jwt_claims(token: &str) -> Option<Value> {
    let mut parts = token.split('.');
    let _header = parts.next()?;
    let payload = parts.next()?;
    let decoded = URL_SAFE_NO_PAD.decode(payload).ok()?;
    serde_json::from_slice(&decoded).ok()
}

fn token_is_expired(token: &str) -> bool {
    if let Some(claims) = jwt_claims(token) {
        if let Some(exp) = claims.get("exp").and_then(|v| v.as_i64()) {
            return exp <= Utc::now().timestamp();
        }
    }
    true
}

fn dedupe_credentials(candidates: Vec<(String, String)>) -> Vec<(String, String, String)> {
    let mut map: HashMap<String, (String, String, bool)> = HashMap::new();
    for (label, token) in candidates {
        let Some(claims) = jwt_claims(&token) else {
            continue;
        };
        let account_id = claims
            .get("https://api.openai.com/auth")
            .and_then(|a| a.get("chatgpt_account_id"))
            .and_then(|v| v.as_str());

        let Some(acc_id) = account_id else {
            continue;
        };

        let expired = token_is_expired(&token);
        if let Some((_, _, old_expired)) = map.get(acc_id) {
            if *old_expired && !expired {
                map.insert(acc_id.to_string(), (label, token, expired));
            }
        } else {
            map.insert(acc_id.to_string(), (label, token, expired));
        }
    }

    map.into_iter()
        .map(|(acc_id, (label, token, _))| (label, token, acc_id))
        .collect()
}

fn codex_credentials() -> Vec<(String, String, String)> {
    let mut candidates = Vec::new();
    let home = match home_dir() {
        Some(h) => h,
        None => return Vec::new(),
    };

    // 1. ~/.codex/auth.json
    let codex_auth_path = home.join(".codex").join("auth.json");
    if let Ok(c) = fs::read_to_string(&codex_auth_path) {
        if let Ok(v) = serde_json::from_str::<Value>(&c) {
            if let Some(tok) = v
                .get("tokens")
                .and_then(|t| t.get("access_token"))
                .and_then(|t| t.as_str())
            {
                candidates.push(("Codex".to_string(), tok.to_string()));
            }
        }
    }

    // 2. Hermes auth.json
    let hermes_auth_path = std::env::var("HERMES_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|_| {
            home.join("AppData")
                .join("Local")
                .join("hermes")
                .join("auth.json")
        });
    if let Ok(c) = fs::read_to_string(&hermes_auth_path) {
        if let Ok(v) = serde_json::from_str::<Value>(&c) {
            if let Some(tok) = v
                .get("providers")
                .and_then(|p| p.get("openai-codex"))
                .and_then(|p| p.get("tokens"))
                .and_then(|t| t.get("access_token"))
                .and_then(|t| t.as_str())
            {
                candidates.push(("Hermes".to_string(), tok.to_string()));
            }
        }
    }

    // 3. OpenCode auth.json (Direct local reading, 0 subprocesses)
    let opencode_auth_path = home
        .join(".local")
        .join("share")
        .join("opencode")
        .join("auth.json");
    if let Ok(c) = fs::read_to_string(&opencode_auth_path) {
        if let Ok(v) = serde_json::from_str::<Value>(&c) {
            if let Some(tok) = v
                .get("openai")
                .and_then(|p| p.get("access"))
                .and_then(|t| t.as_str())
            {
                candidates.push(("OpenCode".to_string(), tok.to_string()));
            }
        }
    }

    dedupe_credentials(candidates)
}

fn normalise_window(window: &Value, default_seconds: i64) -> CodexWindow {
    let used_percent = window.get("used_percent").and_then(|v| v.as_f64());
    let remaining_percent = used_percent.map(|u| ((100.0 - u).max(0.0) * 10.0).round() / 10.0);
    let resets_at = window.get("reset_at").and_then(|v| v.as_i64());
    let seconds = window
        .get("limit_window_seconds")
        .and_then(|v| v.as_i64())
        .unwrap_or(default_seconds);

    let resets_at_iso = resets_at.and_then(|ts| {
        DateTime::from_timestamp(ts, 0).map(|dt| dt.to_rfc3339())
    });

    CodexWindow {
        used_percent,
        remaining_percent,
        window_hours: seconds as f64 / 3600.0,
        resets_at,
        resets_at_iso,
        resets_in: Some(format_countdown_unix(resets_at)),
    }
}

async fn fetch_account_usage(
    client: &reqwest::Client,
    label: &str,
    token: &str,
    account_id: &str,
) -> Result<CodexAccount, String> {
    let claims = jwt_claims(token).unwrap_or_default();
    let email = claims.get("email").and_then(|v| v.as_str()).map(|s| s.to_string());

    let resp = client
        .get("https://chatgpt.com/backend-api/wham/usage")
        .header("Authorization", format!("Bearer {}", token))
        .header("ChatGPT-Account-ID", account_id)
        .header("Accept", "application/json")
        .header("User-Agent", "ai-usage-monitor/1.0")
        .send()
        .await
        .map_err(|e| format!("HTTP request error: {}", e))?;

    if !resp.status().is_success() {
        return Err(format!("HTTP {}", resp.status()));
    }

    let body: Value = resp
        .json()
        .await
        .map_err(|e| format!("JSON parse error: {}", e))?;

    let plan_type = body
        .get("plan_type")
        .and_then(|v| v.as_str())
        .map(|s| s.to_string());

    let rate_limit = body.get("rate_limit").cloned().unwrap_or(body.clone());

    let primary = rate_limit.get("primary_window").map(|w| normalise_window(w, 5 * 3600));
    let secondary = rate_limit
        .get("secondary_window")
        .map(|w| normalise_window(w, 7 * 24 * 3600));

    Ok(CodexAccount {
        label: label.to_string(),
        account_id: Some(account_id.to_string()),
        email,
        plan_type,
        primary,
        secondary,
        status: Some("ok".to_string()),
        error: None,
    })
}

pub async fn fetch_codex_quota(client: &reqwest::Client) -> ServiceResult<CodexData> {
    let creds = codex_credentials();
    if creds.is_empty() {
        return ServiceResult {
            status: "error".to_string(),
            data: None,
            source: Some("Codex local".to_string()),
            error: Some("No se encontraron credenciales de Codex".to_string()),
        };
    }

    let mut accounts = Vec::new();
    for (label, token, account_id) in creds {
        match fetch_account_usage(client, &label, &token, &account_id).await {
            Ok(acc) => accounts.push(acc),
            Err(e) => accounts.push(CodexAccount {
                label,
                account_id: Some(account_id),
                email: None,
                plan_type: None,
                primary: None,
                secondary: None,
                status: Some("error".to_string()),
                error: Some(e),
            }),
        }
    }

    let valid_accounts: Vec<&CodexAccount> = accounts.iter().filter(|a| a.status.as_deref() == Some("ok")).collect();
    if valid_accounts.is_empty() {
        return ServiceResult {
            status: "error".to_string(),
            data: Some(CodexData {
                summary: None,
                accounts,
            }),
            source: Some("Codex live".to_string()),
            error: Some("Error al consultar todas las cuentas de Codex".to_string()),
        };
    }

    // Find summary: account with minimum remaining percent
    let min_account = valid_accounts
        .into_iter()
        .min_by(|a, b| {
            let p_a = a.primary.as_ref().and_then(|p| p.remaining_percent).unwrap_or(100.0);
            let p_b = b.primary.as_ref().and_then(|p| p.remaining_percent).unwrap_or(100.0);
            p_a.partial_cmp(&p_b).unwrap_or(std::cmp::Ordering::Equal)
        })
        .cloned();

    ServiceResult {
        status: "ok".to_string(),
        data: Some(CodexData {
            summary: min_account,
            accounts,
        }),
        source: Some("Codex live".to_string()),
        error: None,
    }
}
