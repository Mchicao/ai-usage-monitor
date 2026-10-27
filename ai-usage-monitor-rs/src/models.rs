use chrono::{DateTime, Local, Utc};
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
    #[serde(default)]
    pub credits: Vec<ResetCredit>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ResetCredit {
    pub id: Option<String>,
    pub expires_at: Option<String>,
}

impl ResetCredits {
    pub fn display_lines(&self) -> Vec<String> {
        let mut lines = vec![format!("{} resets disponibles", self.available_count.unwrap_or(0))];
        for credit in &self.credits {
            lines.push(format!("- Reset {}", format_local_datetime(credit.expires_at.as_deref())));
        }
        lines
    }
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
    pub remaining_percent: Option<f64>,
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
    pub reset_credits: ZaiResets,
    pub account_id: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ZaiReset {
    pub kind: String,
    pub expires_at: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ZaiResets {
    pub status: String,
    pub credits: Vec<ZaiReset>,
    pub error: Option<String>,
}

impl ZaiResets {
    pub fn unavailable(error: &str) -> Self {
        Self { status: "error".into(), credits: Vec::new(), error: Some(error.into()) }
    }

    pub fn display_lines(&self) -> Vec<String> {
        if self.status != "ok" { return vec![self.error.clone().unwrap_or_else(|| "Tarjetas no disponibles".into())]; }
        let mut groups = std::collections::BTreeMap::new();
        for credit in &self.credits {
            *groups.entry((&credit.expires_at, &credit.kind)).or_insert(0) += 1;
        }
        let mut lines = vec![format!("{} resets manuales", self.credits.len())];
        for ((expires, kind), count) in groups {
            let window = if kind == "coding_5h" { "5h" } else { "7d" };
            lines.push(format!("{} × Reset {} {}", count, window, format_local_datetime(Some(expires))));
        }
        lines
    }
}

impl ZaiData {
    pub fn display_lines(&self) -> Vec<String> {
        let mut lines = vec![self.level.as_deref().unwrap_or("Coding Plan").to_uppercase()];
        for limit in &self.limits {
            if limit.kind == "mcp_tools" {
                if let Some(remaining) = limit.remaining_percent { lines.push(format!("MCP: {:.0}% restante", remaining)); }
            } else if limit.kind == "coding_weekly" {
                if let Some(remaining) = limit.remaining_percent { lines.push(format!("7d: {:.0}% restante", remaining)); }
                if limit.reset_at.is_some() { lines.push(format!("Reinicio 7d: {}", format_local_datetime(limit.reset_at.as_deref()))); }
            } else if limit.kind == "coding_5h" {
                lines.push(if limit.reset_at.is_some() {
                    format!("Reinicio 5h: {}", format_local_datetime(limit.reset_at.as_deref()))
                } else { "Reinicio 5h: no informado".into() });
            }
        }
        lines.extend(self.reset_credits.display_lines());
        lines
    }
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

pub fn format_local_datetime(timestamp: Option<&str>) -> String {
    timestamp
        .and_then(|value| DateTime::parse_from_rfc3339(value).ok())
        .map(|value| value.with_timezone(&Local).format("%d/%m %H:%M (hora de Chile)").to_string())
        .unwrap_or_else(|| "Hora no disponible".to_string())
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
