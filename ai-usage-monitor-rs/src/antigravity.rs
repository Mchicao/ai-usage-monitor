use std::path::PathBuf;
use std::{env, fs};

use chrono::Utc;
use serde_json::Value;

use crate::models::{format_countdown, AntigravityData, QuotaGroup, ServiceResult};

fn find_antigravity_oauth_keys() -> (Option<String>, Option<String>) {
    if let (Ok(cid), Ok(csec)) = (
        env::var("ANTIGRAVITY_OAUTH_CLIENT_ID"),
        env::var("ANTIGRAVITY_OAUTH_CLIENT_SECRET"),
    ) {
        return (Some(cid), Some(csec));
    }

    if let Ok(appdata) = env::var("APPDATA") {
        let npm_path = PathBuf::from(appdata)
            .join("npm")
            .join("node_modules")
            .join("antigravity-usage")
            .join("dist")
            .join("index.js");

        if let Ok(content) = fs::read_to_string(npm_path) {
            let mut found_id = None;
            let mut found_sec = None;

            for line in content.split('\n') {
                if line.contains("clientId:") && found_id.is_none() {
                    if let Some(pos) = line.rfind('"') {
                        if let Some(start) = line[..pos].rfind('"') {
                            found_id = Some(line[(start + 1)..pos].to_string());
                        }
                    }
                }
                if line.contains("clientSecret:") && found_sec.is_none() {
                    if let Some(pos) = line.rfind('"') {
                        if let Some(start) = line[..pos].rfind('"') {
                            found_sec = Some(line[(start + 1)..pos].to_string());
                        }
                    }
                }
            }

            if found_id.is_some() && found_sec.is_some() {
                return (found_id, found_sec);
            }
        }
    }

    (None, None)
}

fn find_refresh_token() -> Option<String> {
    let appdata = env::var("APPDATA").ok()?;
    let accounts_dir = PathBuf::from(appdata).join("antigravity-usage").join("accounts");
    if !accounts_dir.is_dir() {
        return None;
    }
    let entries = fs::read_dir(accounts_dir).ok()?;
    for entry in entries.flatten() {
        let tpath = entry.path().join("tokens.json");
        if tpath.is_file() {
            if let Ok(content) = fs::read_to_string(tpath) {
                if let Ok(val) = serde_json::from_str::<Value>(&content) {
                    if let Some(tok) = val.get("refreshToken").or_else(|| val.get("refresh_token")) {
                        if let Some(s) = tok.as_str() {
                            return Some(s.to_string());
                        }
                    }
                }
            }
        }
    }
    None
}

fn antigravity_family(label: &str) -> String {
    let name = label.to_lowercase();
    if name.contains("argon") {
        "Gemini 4 Argon".to_string()
    } else if name.contains("gemini") {
        "Gemini".to_string()
    } else if name.contains("claude") || name.contains("gpt") {
        "Claude y GPT".to_string()
    } else {
        label.trim().to_string()
    }
}

fn fetch_agy_cli() -> Option<AntigravityData> {
    let mut cmd = std::process::Command::new("agy");
    cmd.args(["--print", "/usage"]);
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x08000000); // CREATE_NO_WINDOW
    }

    let output = cmd.output().ok()?;
    if !output.status.success() {
        return None;
    }

    let text = String::from_utf8_lossy(&output.stdout);
    let mut map: std::collections::BTreeMap<String, QuotaGroup> = std::collections::BTreeMap::new();

    for line in text.lines() {
        let fields: Vec<&str> = line.split('\t').map(|s| s.trim()).collect();
        if fields.len() < 3 {
            continue;
        }
        let family_raw = fields[0];
        let window_raw = fields[1].to_lowercase();
        let remaining_raw = fields[2];
        let reset_at = if fields.len() > 3 && !fields[3].is_empty() {
            Some(fields[3].to_string())
        } else {
            None
        };

        let pct = remaining_raw
            .trim_end_matches('%')
            .trim()
            .parse::<f64>()
            .ok()?;

        let family = if family_raw.to_lowercase().contains("argon") {
            "Gemini 4 Argon".to_string()
        } else if family_raw.to_lowercase().contains("gemini") {
            "Gemini".to_string()
        } else if family_raw.to_lowercase().contains("claude") || family_raw.to_lowercase().contains("gpt") {
            "Claude y GPT".to_string()
        } else {
            continue; // Only Gemini, Claude, and Argon!
        };

        let group = map.entry(family.clone()).or_insert_with(|| QuotaGroup {
            label: family,
            remaining_5h_percent: None,
            reset_5h_at: None,
            reset_5h_in: None,
            weekly_remaining_percent: None,
            reset_weekly_at: None,
            reset_weekly_in: None,
            model_count: 0,
        });

        if window_raw.contains("weekly") {
            group.weekly_remaining_percent = Some(pct);
            group.reset_weekly_at = reset_at.clone();
            group.reset_weekly_in = Some(format_countdown(reset_at.as_deref()));
        } else if window_raw.contains("five hour") {
            group.remaining_5h_percent = Some(pct);
            group.reset_5h_at = reset_at.clone();
            group.reset_5h_in = Some(format_countdown(reset_at.as_deref()));
        }
    }

    if map.is_empty() {
        return None;
    }

    let mut groups = Vec::new();
    if let Some(g) = map.remove("Claude y GPT") { groups.push(g); }
    if let Some(g) = map.remove("Gemini") { groups.push(g); }
    if let Some(g) = map.remove("Gemini 4 Argon") { groups.push(g); }

    Some(AntigravityData {
        timestamp: Utc::now().to_rfc3339(),
        method: "agy".to_string(),
        groups,
        stale: false,
    })
}

pub async fn fetch_antigravity_quota(client: &reqwest::Client) -> ServiceResult<AntigravityData> {
    // 1. Try agy --print /usage first (clean, fast, official groups including Argon)
    if let Some(data) = fetch_agy_cli() {
        return ServiceResult {
            status: "ok".to_string(),
            data: Some(data),
            source: Some("agy /usage".to_string()),
            error: None,
        };
    }

    // 2. Fallback: Cloudcode API directly
    let Some(refresh_token) = find_refresh_token() else {
        return ServiceResult {
            status: "error".to_string(),
            data: None,
            source: Some("cloudcode API".to_string()),
            error: Some("No se encontró token de antigravity".to_string()),
        };
    };

    let (client_id_opt, client_secret_opt) = find_antigravity_oauth_keys();
    let (Some(client_id), Some(client_secret)) = (client_id_opt, client_secret_opt) else {
        return ServiceResult {
            status: "error".to_string(),
            data: None,
            source: Some("cloudcode API".to_string()),
            error: Some("No se encontraron claves de cliente OAuth para Antigravity".to_string()),
        };
    };

    let grant_type = "refresh_token".to_string();

    // Refresh Access Token
    let token_resp = client
        .post("https://oauth2.googleapis.com/token")
        .form(&[
            ("client_id", &client_id),
            ("client_secret", &client_secret),
            ("grant_type", &grant_type),
            ("refresh_token", &refresh_token),
        ])
        .send()
        .await;

    let token_json: Value = match token_resp {
        Ok(res) => match res.json().await {
            Ok(j) => j,
            Err(e) => {
                return ServiceResult {
                    status: "error".to_string(),
                    data: None,
                    source: Some("cloudcode API".to_string()),
                    error: Some(format!("Error parseando token: {}", e)),
                }
            }
        },
        Err(e) => {
            return ServiceResult {
                status: "error".to_string(),
                data: None,
                source: Some("cloudcode API".to_string()),
                error: Some(format!("Error solicitando token: {}", e)),
            }
        }
    };

    let Some(access_token) = token_json.get("access_token").and_then(|v| v.as_str()) else {
        return ServiceResult {
            status: "error".to_string(),
            data: None,
            source: Some("cloudcode API".to_string()),
            error: Some("No vino access_token".to_string()),
        };
    };

    // loadCodeAssist -> projectId
    let load_body = serde_json::json!({
        "metadata": {
            "ideType": "ANTIGRAVITY"
        }
    });

    let mut project_id = String::new();
    if let Ok(resp) = client
        .post("https://cloudcode-pa.googleapis.com/v1internal:loadCodeAssist")
        .header("Authorization", format!("Bearer {}", access_token))
        .header("User-Agent", "antigravity")
        .json(&load_body)
        .send()
        .await
    {
        if let Ok(load_res) = resp.json::<Value>().await {
            if let Some(pid) = load_res.get("cloudaicompanionProject").and_then(|v| v.as_str()) {
                project_id = pid.to_string();
            }
        }
    }

    // fetchAvailableModels
    let models_body = if !project_id.is_empty() {
        serde_json::json!({ "project": project_id })
    } else {
        serde_json::json!({})
    };

    let models_res: Value = match client
        .post("https://cloudcode-pa.googleapis.com/v1internal:fetchAvailableModels")
        .header("Authorization", format!("Bearer {}", access_token))
        .header("User-Agent", "antigravity")
        .json(&models_body)
        .send()
        .await
    {
        Ok(r) => r.json().await.unwrap_or_default(),
        Err(e) => {
            return ServiceResult {
                status: "error".to_string(),
                data: None,
                source: Some("cloudcode API".to_string()),
                error: Some(format!("Error en fetchAvailableModels: {}", e)),
            }
        }
    };

    let Some(models_map) = models_res.get("models").and_then(|v| v.as_object()) else {
        return ServiceResult {
            status: "error".to_string(),
            data: None,
            source: Some("cloudcode API".to_string()),
            error: Some("No se encontró mapa de modelos".to_string()),
        };
    };

    // Filter strictly to: "Claude y GPT", "Gemini", "Gemini 4 Argon"
    use std::collections::BTreeMap;
    let mut groups_map: BTreeMap<String, QuotaGroup> = BTreeMap::new();

    for (m_id, m_data) in models_map {
        let label_str = m_data
            .get("displayName")
            .and_then(|v| v.as_str())
            .unwrap_or(m_id);

        let family = antigravity_family(label_str);
        if family != "Claude y GPT" && family != "Gemini" && family != "Gemini 4 Argon" {
            continue; // Skip chat_*, tab_*, etc.
        }

        let qi = m_data.get("quotaInfo");
        let remaining_frac = qi.and_then(|q| q.get("remainingFraction")).and_then(|v| v.as_f64());
        let reset_time = qi
            .and_then(|q| q.get("resetTime"))
            .and_then(|v| v.as_str())
            .map(|s| s.to_string());

        let Some(mut remaining) = remaining_frac else {
            continue;
        };
        if remaining > 1.0 {
            remaining /= 100.0;
        }
        if !(0.0..=1.0).contains(&remaining) {
            continue;
        }

        let pct = (remaining * 100.0 * 10.0).round() / 10.0;

        let group = groups_map.entry(family.clone()).or_insert_with(|| QuotaGroup {
            label: family,
            remaining_5h_percent: Some(pct),
            reset_5h_at: reset_time.clone(),
            reset_5h_in: Some(format_countdown(reset_time.as_deref())),
            weekly_remaining_percent: None,
            reset_weekly_at: None,
            reset_weekly_in: None,
            model_count: 0,
        });

        group.model_count += 1;
        // Keep lowest remaining
        if let Some(current) = group.remaining_5h_percent {
            if pct < current {
                group.remaining_5h_percent = Some(pct);
                group.reset_5h_at = reset_time.clone();
                group.reset_5h_in = Some(format_countdown(reset_time.as_deref()));
            }
        }
    }

    let mut groups = Vec::new();
    if let Some(g) = groups_map.remove("Claude y GPT") { groups.push(g); }
    if let Some(g) = groups_map.remove("Gemini") { groups.push(g); }
    if let Some(g) = groups_map.remove("Gemini 4 Argon") { groups.push(g); }

    ServiceResult {
        status: "ok".to_string(),
        data: Some(AntigravityData {
            timestamp: Utc::now().to_rfc3339(),
            method: "cloudcode_api".to_string(),
            groups,
            stale: false,
        }),
        source: Some("cloudcode API".to_string()),
        error: None,
    }
}
