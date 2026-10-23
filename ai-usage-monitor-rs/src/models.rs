use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct QuotaGroup {
    pub label: String,
    pub remaining_5h_percent: Option<f64>,
    pub reset_5h_at: Option<String>,
    pub reset_5h_in: Option<String>,
    pub weekly_remaining_percent: Option<f64>,
    pub reset_weekly_at: Option<String>,
    pub reset_weekly_in: Option<String>,
    pub model_count: usize,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AntigravityData {
    pub timestamp: String,
    pub method: String,
    pub groups: Vec<QuotaGroup>,
    #[serde(default)]
    pub stale: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CodexWindow {
    pub used_percent: Option<f64>,
    pub remaining_percent: Option<f64>,
    pub window_hours: f64,
    pub resets_at: Option<i64>,
    pub resets_at_iso: Option<String>,
    pub resets_in: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CodexAccount {
    pub label: String,
    pub account_id: Option<String>,
    pub email: Option<String>,
    pub plan_type: Option<String>,
    pub primary: Option<CodexWindow>,
    pub secondary: Option<CodexWindow>,
    #[serde(default)]
    pub status: Option<String>,
    #[serde(default)]
    pub error: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CodexData {
    #[serde(flatten)]
    pub summary: Option<CodexAccount>,
    pub accounts: Vec<CodexAccount>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ZaiLimit {
    pub kind: String,
    pub remaining_percent: f64,
    pub current_prompts: Option<i64>,
    pub max_prompts: Option<i64>,
    pub reset_at: Option<String>,
    pub reset_in: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ZaiData {
    pub level: Option<String>,
    pub limits: Vec<ZaiLimit>,
    pub email: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ServiceResult<T> {
    pub status: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub data: Option<T>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub source: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AllQuotasSnapshot {
    pub antigravity: ServiceResult<AntigravityData>,
    pub codex: ServiceResult<CodexData>,
    pub zai: ServiceResult<ZaiData>,
    pub timestamp: String,
}

pub fn format_countdown(reset_iso: Option<&str>) -> String {
    let Some(iso) = reset_iso else {
        return "N/A".to_string();
    };
    let Ok(target) = DateTime::parse_from_rfc3339(iso) else {
        return "N/A".to_string();
    };
    let now = Utc::now();
    let diff = target.signed_duration_since(now);
    let total_secs = diff.num_seconds();
    if total_secs <= 0 {
        return "0m".to_string();
    }
    let total_mins = total_secs / 60;
    let days = total_mins / (24 * 60);
    let hours = (total_mins % (24 * 60)) / 60;
    let mins = total_mins % 60;

    if days > 0 {
        format!("{}d {}h", days, hours)
    } else {
        format!("{}h {}m", hours, mins)
    }
}

pub fn format_countdown_unix(ts: Option<i64>) -> String {
    let Some(ts_val) = ts else {
        return "N/A".to_string();
    };
    let now = Utc::now().timestamp();
    let diff = ts_val - now;
    if diff <= 0 {
        return "0m".to_string();
    }
    let total_mins = diff / 60;
    let days = total_mins / (24 * 60);
    let hours = (total_mins % (24 * 60)) / 60;
    let mins = total_mins % 60;

    if days > 0 {
        format!("{}d {}h", days, hours)
    } else {
        format!("{}h {}m", hours, mins)
    }
}
