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
pub struct ResetCredits {
    pub available_count: Option<i64>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CodexAccount {
    pub label: String,
    pub account_id: Option<String>,
    pub email: Option<String>,
    pub plan_type: Option<String>,
    pub primary: Option<CodexWindow>,
    pub secondary: Option<CodexWindow>,
    pub reset_credits: Option<ResetCredits>,
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
    pub data: Option<T>,
    pub source: Option<String>,
    #[serde(default)]
    pub error: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AllQuotasSnapshot {
    pub antigravity: ServiceResult<AntigravityData>,
    pub codex: ServiceResult<CodexData>,
    pub zai: ServiceResult<ZaiData>,
    pub timestamp: String,
}

pub fn format_countdown(iso_str: Option<&str>) -> String {
    let Some(raw) = iso_str else {
        return "N/A".to_string();
    };

    let dt = if let Ok(d) = DateTime::parse_from_rfc3339(raw) {
        d.with_timezone(&Utc)
    } else {
        return "N/A".to_string();
    };

    let now = Utc::now();
    let diff = dt.signed_duration_since(now);
    let total_secs = diff.num_seconds();

    if total_secs <= 0 {
        return "Ya reseteado".to_string();
    }

    let hours = total_secs / 3600;
    let mins = (total_secs % 3600) / 60;

    if hours >= 24 {
        let days = hours / 24;
        let rem_hours = hours % 24;
        format!("{}d {}h", days, rem_hours)
    } else {
        format!("{}h {}m", hours, mins)
    }
}

pub fn format_countdown_unix(ts_opt: Option<i64>) -> String {
    let Some(ts) = ts_opt else {
        return "N/A".to_string();
    };

    let Some(dt) = DateTime::from_timestamp(ts, 0) else {
        return "N/A".to_string();
    };

    let now = Utc::now();
    let diff = dt.signed_duration_since(now);
    let total_secs = diff.num_seconds();

    if total_secs <= 0 {
        return "Ya reseteado".to_string();
    }

    let hours = total_secs / 3600;
    let mins = (total_secs % 3600) / 60;

    if hours >= 24 {
        let days = hours / 24;
        let rem_hours = hours % 24;
        format!("{}d {}h", days, rem_hours)
    } else {
        format!("{}h {}m", hours, mins)
    }
}
