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
    for y in 4..28 {
        for i in 0..3 {
            let x_start = 4 + i * 9;
            for x in x_start..(x_start + 6) {
                img.put_pixel(x, y, Rgba([48, 54, 61, 255]));
            }
        }
    }
    unsafe { create_hicon_from_rgba(&img.into_raw(), size as i32, size as i32) }
}

pub fn create_tray_hicon(snapshot: &AllQuotasSnapshot) -> Option<HICON> {
    let size = 32u32;
    let mut img = RgbaImage::new(size, size);

    // 1. Antigravity percent (lowest among groups)
    let mut ag_pct: Option<f64> = None;
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

    // 2. Codex percent (lowest across accounts and windows)
    let mut codex_pct: Option<f64> = None;
    if let Some(ref c) = snapshot.codex.data {
        let mut pcts = Vec::new();
        for acct in &c.accounts {
            if let Some(ref p) = acct.primary {
                if let Some(rem) = p.remaining_percent {
                    pcts.push(rem);
                }
            }
            if let Some(ref s) = acct.secondary {
                if let Some(rem) = s.remaining_percent {
                    pcts.push(rem);
                }
            }
        }
        if !pcts.is_empty() {
            codex_pct = pcts.into_iter().min_by(|a, b| a.partial_cmp(b).unwrap());
        }
    }

    // 3. ZAI percent
    let mut zai_pct: Option<f64> = None;
    if let Some(ref z) = snapshot.zai.data {
        for lim in &z.limits {
            if lim.kind == "coding_5h" {
                zai_pct = lim.remaining_percent;
                break;
            }
        }
    }

    let bars = [ag_pct, codex_pct, zai_pct];
    let bar_w = 6u32;
    let bar_h = 24u32;
    let start_x = 4u32;
    let gap = 9u32;
    let start_y = 4u32;

    for (i, pct_opt) in bars.iter().enumerate() {
        let x_pos = start_x + (i as u32) * gap;

        // Draw background bar
        for y in start_y..(start_y + bar_h) {
            for x in x_pos..(x_pos + bar_w) {
                img.put_pixel(x, y, Rgba([48, 54, 61, 255]));
            }
        }

        // Draw filled bar from bottom
        if let Some(pct) = pct_opt {
            let color = if *pct > 50.0 {
                Rgba([63, 185, 80, 255]) // Green
            } else if *pct > 20.0 {
                Rgba([210, 153, 34, 255]) // Yellow
            } else {
                Rgba([248, 81, 73, 255]) // Red
            };

            let fill_h = ((bar_h as f64) * (pct.clamp(0.0, 100.0) / 100.0)).round() as u32;
            if fill_h > 0 {
                let fill_start_y = start_y + bar_h - fill_h;
                for y in fill_start_y..(start_y + bar_h) {
                    for x in x_pos..(x_pos + bar_w) {
                        img.put_pixel(x, y, color);
                    }
                }
            }
        }
    }

    unsafe { create_hicon_from_rgba(&img.into_raw(), size as i32, size as i32) }
}

pub fn create_tooltip(snapshot: &AllQuotasSnapshot) -> String {
    // AG
    let mut ag_t = "AG:N/A".to_string();
    if let Some(ref a) = snapshot.antigravity.data {
        let mut pcts = Vec::new();
        for g in &a.groups {
            if let Some(p) = g.remaining_5h_percent {
                pcts.push((p, g.reset_5h_in.as_deref()));
            } else if let Some(p) = g.weekly_remaining_percent {
                pcts.push((p, g.reset_weekly_in.as_deref()));
            }
        }
        if let Some((min_val, rst)) = pcts.into_iter().min_by(|a, b| a.0.partial_cmp(&b.0).unwrap()) {
            if let Some(r) = rst {
                if r != "N/A" && r != "Ya reseteado" {
                    ag_t = format!("🛸AG:{:.0}%({})", min_val, r);
                } else {
                    ag_t = format!("🛸AG:{:.0}%", min_val);
                }
            } else {
                ag_t = format!("🛸AG:{:.0}%", min_val);
            }
        }
    }

    // CX
    let mut cx_t = "CX:N/A".to_string();
    if let Some(ref c) = snapshot.codex.data {
        let mut parts = Vec::new();
        for acct in &c.accounts {
            let mut pcts = Vec::new();
            if let Some(ref p) = acct.primary {
                if let Some(rem) = p.remaining_percent { pcts.push(rem); }
            }
            if let Some(ref s) = acct.secondary {
                if let Some(rem) = s.remaining_percent { pcts.push(rem); }
            }
            if let Some(min_p) = pcts.into_iter().min_by(|a, b| a.partial_cmp(b).unwrap()) {
                let lbl = if acct.label.len() >= 2 { &acct.label[..2] } else { &acct.label };
                parts.push(format!("{}:{:.0}%", lbl, min_p));
            }
        }
        if !parts.is_empty() {
            cx_t = parts.join(" ");
        }
    }

    // ZI
    let mut zi_t = "ZI:N/A".to_string();
    if let Some(ref z) = snapshot.zai.data {
        for lim in &z.limits {
            if lim.kind == "coding_5h" {
                if let Some(remaining) = lim.remaining_percent {
                    zi_t = format!("🧠ZI:{:.0}%", remaining);
                }
                break;
            }
        }
    }

    let raw = format!("{} | {} | {}\n(Click para detalles)", ag_t, cx_t, zi_t);
    if raw.len() > 125 {
        raw[..125].to_string()
    } else {
        raw
    }
}
