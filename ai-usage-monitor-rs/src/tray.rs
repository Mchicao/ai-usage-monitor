use image::{Rgba, RgbaImage};
use windows::Win32::Graphics::Gdi::*;
use windows::Win32::UI::WindowsAndMessaging::*;

use crate::models::AllQuotasSnapshot;

pub unsafe fn create_hicon_from_rgba(rgba: &[u8], width: i32, height: i32) -> Option<HICON> {
    unsafe {
        let mut bgra = rgba.to_vec();
        for chunk in bgra.chunks_exact_mut(4) {
            chunk.swap(0, 2);
        }

        let hdc = CreateCompatibleDC(None);
        let bmi = BITMAPINFO {
            bmiHeader: BITMAPINFOHEADER {
                biSize: std::mem::size_of::<BITMAPINFOHEADER>() as u32,
                biWidth: width,
                biHeight: -height, // top-down
                biPlanes: 1,
                biBitCount: 32,
                biCompression: BI_RGB.0,
                ..std::mem::zeroed()
            },
            ..std::mem::zeroed()
        };

        let mut bits: *mut std::ffi::c_void = std::ptr::null_mut();
        let color_bmp = match CreateDIBSection(hdc, &bmi, DIB_RGB_COLORS, &mut bits, None, 0) {
            Ok(b) => b,
            Err(_) => {
                let _ = DeleteDC(hdc);
                return None;
            }
        };

        std::ptr::copy_nonoverlapping(bgra.as_ptr(), bits as *mut u8, bgra.len());

        let mask_bmp = CreateBitmap(width, height, 1, 1, None);

        let icon_info = ICONINFO {
            fIcon: true.into(),
            xHotspot: 0,
            yHotspot: 0,
            hbmMask: mask_bmp,
            hbmColor: color_bmp,
        };

        let hicon = CreateIconIndirect(&icon_info).ok();

        let _ = DeleteObject(color_bmp);
        let _ = DeleteObject(mask_bmp);
        let _ = DeleteDC(hdc);

        hicon
    }
}

pub fn create_default_icon() -> Option<HICON> {
    let size = 32u32;
    let mut img = RgbaImage::new(size, size);
    for y in 0..size {
        for x in 0..size {
            let dx = (x as f32) - 15.5;
            let dy = (y as f32) - 15.5;
            if dx * dx + dy * dy <= 14.0 * 14.0 {
                img.put_pixel(x, y, Rgba([88, 166, 255, 255]));
            }
        }
    }
    unsafe { create_hicon_from_rgba(&img.into_raw(), size as i32, size as i32) }
}

pub fn create_tray_hicon(snapshot: &AllQuotasSnapshot) -> Option<HICON> {
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

    let min_pct = match (ag_pct, codex_pct, zai_pct) {
        (Some(a), Some(c), Some(z)) => a.min(c).min(z),
        (Some(a), Some(c), None) => a.min(c),
        (Some(a), None, Some(z)) => a.min(z),
        (None, Some(c), Some(z)) => c.min(z),
        (Some(a), None, None) => a,
        (None, Some(c), None) => c,
        (None, None, Some(z)) => z,
        _ => 100.0,
    };

    let color = if min_pct > 50.0 {
        Rgba([59, 165, 93, 255]) // Green #3ba55d
    } else if min_pct > 20.0 {
        Rgba([245, 158, 11, 255]) // Yellow #f59e0b
    } else {
        Rgba([237, 66, 69, 255]) // Red #ed4245
    };

    let filled_height = ((size as f64) * (min_pct / 100.0)).round() as u32;

    for y in 0..size {
        for x in 0..size {
            let dx = (x as f32) - 15.5;
            let dy = (y as f32) - 15.5;
            let dist_sq = dx * dx + dy * dy;

            if dist_sq <= 15.0 * 15.0 {
                img.put_pixel(x, y, Rgba([30, 30, 30, 255]));

                if y >= size - filled_height && dist_sq <= 13.0 * 13.0 {
                    img.put_pixel(x, y, color);
                }
            }
        }
    }

    unsafe { create_hicon_from_rgba(&img.into_raw(), size as i32, size as i32) }
}

pub fn create_tooltip(snapshot: &AllQuotasSnapshot) -> String {
    let mut lines = Vec::new();

    // Antigravity
    if let Some(ref a) = snapshot.antigravity.data {
        lines.push("Antigravity:".to_string());
        for g in &a.groups {
            let pct = g.remaining_5h_percent
                .map(|p| format!("{:.0}%", p))
                .unwrap_or_else(|| "N/A".to_string());
            let rst = g.reset_5h_in.as_deref().unwrap_or("-");
            lines.push(format!(" • {}: {} ({})", g.label, pct, rst));
        }
    } else {
        lines.push(format!("Antigravity: {}", snapshot.antigravity.status));
    }

    // Codex
    if let Some(ref c) = snapshot.codex.data {
        let mut codex_strs = Vec::new();
        for acct in &c.accounts {
            if let Some(ref p) = acct.primary {
                let pct = p.remaining_percent
                    .map(|v| format!("{:.0}%", v))
                    .unwrap_or_else(|| "N/A".to_string());
                codex_strs.push(format!(" • {}: {}", acct.label, pct));
            }
        }
        if !codex_strs.is_empty() {
            lines.push("Codex:".to_string());
            lines.extend(codex_strs);
        }
    } else {
        lines.push(format!("Codex: {}", snapshot.codex.status));
    }

    // ZAI
    if let Some(ref z) = snapshot.zai.data {
        lines.push("Z.AI:".to_string());
        for l in &z.limits {
            if l.kind == "coding_5h" {
                let rst = l.reset_in.as_deref().unwrap_or("-");
                lines.push(format!(" • Code: {:.0}% ({})", l.remaining_percent, rst));
            }
        }
    } else {
        lines.push(format!("Z.AI: {}", snapshot.zai.status));
    }

    lines.join("\n")
}
