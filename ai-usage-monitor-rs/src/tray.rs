use image::{Rgba, RgbaImage};
use tray_icon::Icon;

use crate::models::AllQuotasSnapshot;

pub fn create_tray_icon(snapshot: &AllQuotasSnapshot) -> Option<Icon> {
    let size = 32u32;
    let mut img = RgbaImage::new(size, size);

    // ZAI percent
    let mut zai_pct = None;
    if let Some(ref z) = snapshot.zai.data {
        for lim in &z.limits {
            if lim.kind == "coding_5h" {
                zai_pct = Some(lim.remaining_percent);
                break;
            }
        }
    }

    // Codex percent
    let mut codex_pct = None;
    if let Some(ref c) = snapshot.codex.data {
        let mut pcts = Vec::new();
        for acct in &c.accounts {
            if let Some(ref p) = acct.primary {
                if let Some(rem) = p.remaining_percent {
                    pcts.push(rem);
                }
            }
        }
        if !pcts.is_empty() {
            codex_pct = pcts.into_iter().min_by(|a, b| a.partial_cmp(b).unwrap());
        }
    }

    // Antigravity percent
    let mut ag_pct = None;
    if let Some(ref a) = snapshot.antigravity.data {
        let mut pcts = Vec::new();
        for g in &a.groups {
            if let Some(val) = g.remaining_5h_percent {
                pcts.push(val);
            } else if let Some(val) = g.weekly_remaining_percent {
                pcts.push(val);
            }
        }
        if !pcts.is_empty() {
            ag_pct = pcts.into_iter().min_by(|a, b| a.partial_cmp(b).unwrap());
        }
    }

    let bars = [ag_pct, codex_pct, zai_pct];
    let bar_w = 6u32;
    let bar_h = 24u32;
    let start_x = 4u32;
    let gap = 9u32;
    let start_y = 4u32;

    for (i, pct_opt) in bars.iter().enumerate() {
        let x = start_x + (i as u32) * gap;
        // Background track (dark grey)
        for px in x..(x + bar_w) {
            for py in start_y..(start_y + bar_h) {
                img.put_pixel(px, py, Rgba([48, 54, 61, 255]));
            }
        }

        if let Some(pct) = pct_opt {
            let color = if *pct > 50.0 {
                Rgba([63, 185, 80, 255]) // Green
            } else if *pct > 20.0 {
                Rgba([210, 153, 34, 255]) // Orange/Yellow
            } else {
                Rgba([248, 81, 73, 255]) // Red
            };

            let fill_h = ((bar_h as f64) * (*pct / 100.0)).round() as u32;
            let fill_h = fill_h.min(bar_h);
            if fill_h > 0 {
                for px in x..(x + bar_w) {
                    for py in (start_y + bar_h - fill_h)..(start_y + bar_h) {
                        img.put_pixel(px, py, color);
                    }
                }
            }
        }
    }

    let rgba_data = img.into_raw();
    Icon::from_rgba(rgba_data, size, size).ok()
}

pub fn create_tooltip(snapshot: &AllQuotasSnapshot) -> String {
    let mut ag_str = "AG:N/A".to_string();
    if let Some(ref a) = snapshot.antigravity.data {
        let mut pcts = Vec::new();
        for g in &a.groups {
            if let Some(val) = g.remaining_5h_percent {
                pcts.push((val, "5h"));
            } else if let Some(val) = g.weekly_remaining_percent {
                pcts.push((val, "7d"));
            }
        }
        if let Some((min_val, win)) = pcts.into_iter().min_by(|a, b| a.0.partial_cmp(&b.0).unwrap()) {
            ag_str = format!("AG:{:.0}% ({})", min_val, win);
        }
    }

    let mut cx_str = "CX:N/A".to_string();
    if let Some(ref c) = snapshot.codex.data {
        let mut pcts = Vec::new();
        for acct in &c.accounts {
            if let Some(ref p) = acct.primary {
                if let Some(val) = p.remaining_percent {
                    pcts.push((val, "5h"));
                }
            }
        }
        if let Some((min_val, win)) = pcts.into_iter().min_by(|a, b| a.0.partial_cmp(&b.0).unwrap()) {
            cx_str = format!("CX:{:.0}% ({})", min_val, win);
        }
    }

    let mut zi_str = "ZI:N/A".to_string();
    if let Some(ref z) = snapshot.zai.data {
        for lim in &z.limits {
            if lim.kind == "coding_5h" {
                zi_str = format!("ZI:{:.0}% (5h)", lim.remaining_percent);
                break;
            }
        }
    }

    format!("{} | {} | {}", ag_str, cx_str, zi_str)
}
