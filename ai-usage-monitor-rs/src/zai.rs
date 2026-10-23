use std::fs;
use std::path::PathBuf;

use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use base64::Engine;
use serde_json::Value;

use crate::models::{format_countdown_unix, ServiceResult, ZaiData, ZaiLimit};

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

pub async fn fetch_zai_quota(client: &reqwest::Client) -> ServiceResult<ZaiData> {
    let home = match home_dir() {
        Some(h) => h,
        None => {
            return ServiceResult {
                status: "error".to_string(),
                data: None,
                source: Some("Z.AI API".to_string()),
                error: Some("No se pudo resolver home dir".to_string()),
            }
        }
    };

    let token_path = home
        .join("AppData")
        .join("Local")
        .join("hermes")
        .join(".zai_platform_token.txt");

    let Ok(token_content) = fs::read_to_string(&token_path) else {
        return ServiceResult {
            status: "error".to_string(),
            data: None,
            source: Some("Z.AI API".to_string()),
            error: Some("No se encontró archivo de token de Z.AI".to_string()),
        };
    };

    let token = token_content.trim();
    if token.is_empty() {
        return ServiceResult {
            status: "error".to_string(),
            data: None,
            source: Some("Z.AI API".to_string()),
            error: Some("Token de Z.AI vacío".to_string()),
        };
    }

    let email = jwt_claims(token)
        .and_then(|c| c.get("customer").cloned())
        .and_then(|cust| cust.get("email").and_then(|v| v.as_str()).map(|s| s.to_string()));

    let org = "org-2363d4ce091a445D936A7D3B5EB0C57C";
    let proj = "proj_F7F3eC68baF047658a790bcAA12B68d4";

    let resp = client
        .get("https://api.z.ai/api/monitor/usage/quota/limit")
        .header("Authorization", format!("Bearer {}", token))
        .header("bigmodel-organization", org)
        .header("bigmodel-project", proj)
        .header("Accept", "application/json")
        .send()
        .await;

    let body: Value = match resp {
        Ok(r) if r.status().is_success() => match r.json().await {
            Ok(j) => j,
            Err(e) => {
                return ServiceResult {
                    status: "error".to_string(),
                    data: None,
                    source: Some("Z.AI API".to_string()),
                    error: Some(format!("Error parseando respuesta Z.AI: {}", e)),
                }
            }
        },
        Ok(r) => {
            return ServiceResult {
                status: "error".to_string(),
                data: None,
                source: Some("Z.AI API".to_string()),
                error: Some(format!("HTTP {}", r.status())),
            }
        }
        Err(e) => {
            return ServiceResult {
                status: "error".to_string(),
                data: None,
                source: Some("Z.AI API".to_string()),
                error: Some(format!("Error conectando con Z.AI: {}", e)),
            }
        }
    };

    let data_obj = body.get("data").and_then(|v| v.as_object());
    let level = data_obj
        .and_then(|d| d.get("level"))
        .and_then(|v| v.as_str())
        .map(|s| s.to_string());

    let raw_limits = data_obj
        .and_then(|d| d.get("limits"))
        .and_then(|v| v.as_array());

    let mut limits = Vec::new();
    if let Some(arr) = raw_limits {
        for item in arr {
            let limit_type = item.get("type").and_then(|v| v.as_str()).unwrap_or("");
            let raw_used = item.get("percentage").and_then(|v| v.as_f64());
            let raw_remaining = item.get("remaining").and_then(|v| v.as_f64());

            let remaining = if let Some(r) = raw_remaining {
                r
            } else if let Some(u) = raw_used {
                100.0 - u
            } else {
                0.0
            };

            let details = item.get("usageDetails").and_then(|v| v.as_array());
            let kind = if limit_type == "TOKENS_LIMIT" {
                "coding_5h"
            } else if limit_type == "TIME_LIMIT" && details.is_some() {
                "mcp_tools"
            } else {
                "other"
            };

            let next_reset_ms = item.get("nextResetTime").and_then(|v| v.as_i64());
            let reset_in = next_reset_ms.map(|ms| format_countdown_unix(Some(ms / 1000)));

            limits.push(ZaiLimit {
                kind: kind.to_string(),
                remaining_percent: remaining,
                current_prompts: item.get("currentValue").and_then(|v| v.as_i64()),
                max_prompts: item.get("usage").and_then(|v| v.as_i64()),
                reset_at: None,
                reset_in,
            });
        }
    }

    ServiceResult {
        status: "ok".to_string(),
        data: Some(ZaiData {
            level,
            limits,
            email,
        }),
        source: Some("Z.AI API".to_string()),
        error: None,
    }
}
