use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Mutex;
use windows::core::{w, PCWSTR};
use windows::Win32::Foundation::*;
use windows::Win32::Graphics::Dwm::{
    DwmSetWindowAttribute, DWMWA_WINDOW_CORNER_PREFERENCE, DWMWCP_ROUND,
};
use windows::Win32::Graphics::Gdi::*;
use windows::Win32::System::LibraryLoader::GetModuleHandleW;
use windows::Win32::UI::WindowsAndMessaging::*;

use crate::models::AllQuotasSnapshot;

static POPUP_HWND: AtomicUsize = AtomicUsize::new(0);
static POPUP_SNAPSHOT: Mutex<Option<AllQuotasSnapshot>> = Mutex::new(None);

// Colors in BGR format for Win32 COLORREF
const COLOR_BG: u32 = 0x1A1A1A;       // #1a1a1a
const COLOR_CARD: u32 = 0x2B2B2B;     // #2b2b2b
const COLOR_BAR_BG: u32 = 0x3D3D3D;   // #3d3d3d
const COLOR_TEXT: u32 = 0xFFFFFF;     // #ffffff
const COLOR_MUTED: u32 = 0x808080;    // #808080
const COLOR_GREEN: u32 = 0x43A02E;    // #2ea043 -> BGR: 0x43A02E
const COLOR_YELLOW: u32 = 0x2299D2;   // #d29922 -> BGR: 0x2299D2
const COLOR_RED: u32 = 0x4951F8;      // #f85149 -> BGR: 0x4951F8
const COLOR_ACCENT: u32 = 0xFFA658;   // #58a6ff -> BGR: 0xFFA658

fn pct_color(pct: Option<f64>) -> u32 {
    match pct {
        Some(p) if p > 50.0 => COLOR_GREEN,
        Some(p) if p > 20.0 => COLOR_YELLOW,
        Some(_) => COLOR_RED,
        None => COLOR_MUTED,
    }
}

pub fn get_popup_hwnd() -> Option<HWND> {
    let raw = POPUP_HWND.load(Ordering::SeqCst);
    if raw != 0 {
        Some(HWND(raw as *mut _))
    } else {
        None
    }
}

pub fn init_popup() -> HWND {
    unsafe {
        let hinstance = GetModuleHandleW(None).unwrap();
        let class_name: Vec<u16> = "ai_usage_monitor_fluent_popup\0".encode_utf16().collect();

        let wnd_class = WNDCLASSW {
            style: CS_HREDRAW | CS_VREDRAW,
            lpfnWndProc: Some(popup_wnd_proc),
            cbClsExtra: 0,
            cbWndExtra: 0,
            hInstance: hinstance.into(),
            hIcon: HICON::default(),
            hCursor: LoadCursorW(None, IDC_ARROW).unwrap_or_default(),
            hbrBackground: HBRUSH::default(),
            lpszMenuName: PCWSTR::null(),
            lpszClassName: PCWSTR(class_name.as_ptr()),
        };

        let _ = RegisterClassW(&wnd_class);

        let hwnd = CreateWindowExW(
            WS_EX_TOPMOST | WS_EX_TOOLWINDOW,
            PCWSTR(class_name.as_ptr()),
            w!("AIUsageMonitorPopup"),
            WS_POPUP,
            0,
            0,
            350,
            500,
            None,
            None,
            hinstance,
            None,
        ).expect("Failed to create popup window");

        // Windows 11 rounded corners
        let corner_pref = DWMWCP_ROUND;
        let _ = DwmSetWindowAttribute(
            hwnd,
            DWMWA_WINDOW_CORNER_PREFERENCE,
            &corner_pref as *const _ as *const _,
            std::mem::size_of_val(&corner_pref) as u32,
        );

        POPUP_HWND.store(hwnd.0 as usize, Ordering::SeqCst);
        hwnd
    }
}

pub fn toggle_popup() {
    if let Some(hwnd) = get_popup_hwnd() {
        unsafe {
            if IsWindowVisible(hwnd).as_bool() {
                let _ = ShowWindow(hwnd, SW_HIDE);
                return;
            }
        }
    }
    show_popup();
}

pub fn hide_popup() {
    if let Some(hwnd) = get_popup_hwnd() {
        unsafe {
            let _ = ShowWindow(hwnd, SW_HIDE);
        }
    }
}

pub fn show_popup() {
    let Some(hwnd) = get_popup_hwnd() else { return; };
    let h = calculate_content_height();

    unsafe {
        let mut wa = RECT::default();
        let _ = SystemParametersInfoW(
            SPI_GETWORKAREA,
            0,
            Some(&mut wa as *mut _ as *mut _),
            SYSTEM_PARAMETERS_INFO_UPDATE_FLAGS(0),
        );

        let width = 350;
        let x = wa.right - width - 16;
        let y = wa.bottom - h - 12;

        let _ = SetWindowPos(
            hwnd,
            HWND_TOPMOST,
            x,
            y,
            width,
            h,
            SWP_SHOWWINDOW,
        );

        let _ = SetForegroundWindow(hwnd);
        let _ = InvalidateRect(hwnd, None, true);
    }
}

pub fn update_popup_data(snapshot: &AllQuotasSnapshot) {
    if let Ok(mut lock) = POPUP_SNAPSHOT.lock() {
        *lock = Some(snapshot.clone());
    }

    if let Some(hwnd) = get_popup_hwnd() {
        let is_vis = unsafe { IsWindowVisible(hwnd).as_bool() };
        if is_vis {
            show_popup();
        }
    }
}

fn calculate_content_height() -> i32 {
    let mut h = 56; // Header height + margins

    let lock = POPUP_SNAPSHOT.lock().unwrap();
    let Some(ref snap) = *lock else {
        return 200;
    };

    // Antigravity cards
    if let Some(ref a) = snap.antigravity.data {
        for _ in &a.groups {
            h += 70;
        }
    }

    // Codex cards
    if let Some(ref c) = snap.codex.data {
        for acct in &c.accounts {
            let mut num_windows = 0;
            if let Some(ref p) = acct.primary {
                if p.remaining_percent.is_some() { num_windows += 1; }
            }
            if let Some(ref s) = acct.secondary {
                if s.remaining_percent.is_some() { num_windows += 1; }
            }
            let has_resets = acct.reset_credits.is_some();
            let credits_extra = if has_resets { 18 } else { 0 };
            h += 44 + num_windows * 30 + 20 + credits_extra + 8;
        }
    }

    // ZAI card
    if snap.zai.data.is_some() {
        h += 70;
    }

    h += 16;
    h.clamp(320, 750)
}

unsafe extern "system" fn popup_wnd_proc(hwnd: HWND, msg: u32, wparam: WPARAM, lparam: LPARAM) -> LRESULT {
    unsafe {
        match msg {
            WM_PAINT => {
                let mut ps = PAINTSTRUCT::default();
                let hdc = BeginPaint(hwnd, &mut ps);

                let mut rect = RECT::default();
                let _ = GetClientRect(hwnd, &mut rect);
                let w = rect.right - rect.left;
                let h = rect.bottom - rect.top;

                // Double buffer
                let mem_dc = CreateCompatibleDC(hdc);
                let mem_bmp = CreateCompatibleBitmap(hdc, w, h);
                let old_bmp = SelectObject(mem_dc, mem_bmp);

                render_popup_ui(mem_dc, w, h);

                let _ = BitBlt(hdc, 0, 0, w, h, mem_dc, 0, 0, SRCCOPY);

                let _ = SelectObject(mem_dc, old_bmp);
                let _ = DeleteObject(mem_bmp);
                let _ = DeleteDC(mem_dc);

                let _ = EndPaint(hwnd, &ps);
                LRESULT(0)
            }

            WM_ACTIVATE => {
                let active = (wparam.0 & 0xFFFF) as u32;
                if active == WA_INACTIVE {
                    hide_popup();
                }
                LRESULT(0)
            }

            WM_KILLFOCUS => {
                hide_popup();
                LRESULT(0)
            }

            WM_KEYDOWN => {
                if wparam.0 == 0x1B { // VK_ESCAPE
                    hide_popup();
                }
                LRESULT(0)
            }

            WM_LBUTTONDOWN => {
                let x = (lparam.0 & 0xFFFF) as i16 as i32;
                let y = ((lparam.0 >> 16) & 0xFFFF) as i16 as i32;

                // Close button [X] at (w - 38, 14, 24, 24)
                let mut rect = RECT::default();
                let _ = GetClientRect(hwnd, &mut rect);
                let close_x = rect.right - 38;
                let close_y = 14;

                if x >= close_x && x <= close_x + 24 && y >= close_y && y <= close_y + 24 {
                    hide_popup();
                }
                LRESULT(0)
            }

            _ => DefWindowProcW(hwnd, msg, wparam, lparam),
        }
    }
}

unsafe fn create_font(name: &str, size_pt: i32, bold: bool) -> HFONT {
    let name_utf16: Vec<u16> = name.encode_utf16().chain(std::iter::once(0)).collect();
    unsafe {
        CreateFontW(
            -size_pt,
            0,
            0,
            0,
            if bold { 700 } else { 400 },
            0,
            0,
            0,
            DEFAULT_CHARSET.0 as u32,
            OUT_DEFAULT_PRECIS.0 as u32,
            CLIP_DEFAULT_PRECIS.0 as u32,
            CLEARTYPE_QUALITY.0 as u32,
            (DEFAULT_PITCH.0 | FF_DONTCARE.0) as u32,
            PCWSTR(name_utf16.as_ptr()),
        )
    }
}

unsafe fn draw_text_helper(hdc: HDC, text: &str, mut rect: RECT, format: DRAW_TEXT_FORMAT) {
    let mut utf16: Vec<u16> = text.encode_utf16().collect();
    unsafe {
        let _ = DrawTextW(hdc, &mut utf16, &mut rect, format);
    }
}

unsafe fn draw_rounded_card(hdc: HDC, x: i32, y: i32, w: i32, h: i32, bg_color: u32) {
    unsafe {
        let brush = CreateSolidBrush(COLORREF(bg_color));
        let pen = CreatePen(PS_NULL, 0, COLORREF(bg_color));
        let old_brush = SelectObject(hdc, brush);
        let old_pen = SelectObject(hdc, pen);

        let _ = RoundRect(hdc, x, y, x + w, y + h, 14, 14);

        let _ = SelectObject(hdc, old_brush);
        let _ = SelectObject(hdc, old_pen);
        let _ = DeleteObject(brush);
        let _ = DeleteObject(pen);
    }
}

unsafe fn draw_progress_bar(hdc: HDC, x: i32, y: i32, w: i32, h: i32, pct: Option<f64>) {
    unsafe {
        // Background
        let bg_brush = CreateSolidBrush(COLORREF(COLOR_BAR_BG));
        let bg_pen = CreatePen(PS_NULL, 0, COLORREF(COLOR_BAR_BG));
        let old_brush = SelectObject(hdc, bg_brush);
        let old_pen = SelectObject(hdc, bg_pen);
        let _ = RoundRect(hdc, x, y, x + w, y + h, h, h);

        // Filled portion
        if let Some(p) = pct {
            let fill_w = ((w as f64) * (p.clamp(0.0, 100.0) / 100.0)).round() as i32;
            if fill_w > 0 {
                let fg_color = pct_color(Some(p));
                let fg_brush = CreateSolidBrush(COLORREF(fg_color));
                let fg_pen = CreatePen(PS_NULL, 0, COLORREF(fg_color));
                let _ = SelectObject(hdc, fg_brush);
                let _ = SelectObject(hdc, fg_pen);

                let fill_radius = h.min(fill_w);
                let _ = RoundRect(hdc, x, y, x + fill_w, y + h, fill_radius, fill_radius);

                let _ = SelectObject(hdc, old_brush);
                let _ = SelectObject(hdc, old_pen);
                let _ = DeleteObject(fg_brush);
                let _ = DeleteObject(fg_pen);
            }
        }

        let _ = SelectObject(hdc, old_brush);
        let _ = SelectObject(hdc, old_pen);
        let _ = DeleteObject(bg_brush);
        let _ = DeleteObject(bg_pen);
    }
}

unsafe fn render_popup_ui(hdc: HDC, total_w: i32, total_h: i32) {
    unsafe {
        // Fill window background
        let bg_brush = CreateSolidBrush(COLORREF(COLOR_BG));
        let full_rect = RECT { left: 0, top: 0, right: total_w, bottom: total_h };
        let _ = FillRect(hdc, &full_rect, bg_brush);
        let _ = DeleteObject(bg_brush);

        let _ = SetBkMode(hdc, TRANSPARENT);

        // Fonts
        let font_title = create_font("Segoe UI", 20, true);
        let font_card_title = create_font("Segoe UI", 15, true);
        let font_pct = create_font("Segoe UI", 16, true);
        let font_pct_big = create_font("Segoe UI", 20, true);
        let font_sub = create_font("Segoe UI", 13, false);
        let font_win_label = create_font("Segoe UI", 13, true);

        // 1. Header: "⚡ Cuotas IA" + [✕]
        let old_font = SelectObject(hdc, font_title);
        let _ = SetTextColor(hdc, COLORREF(COLOR_TEXT));
        draw_text_helper(
            hdc,
            "⚡ Cuotas IA",
            RECT { left: 18, top: 14, right: total_w - 50, bottom: 42 },
            DT_LEFT | DT_VCENTER | DT_SINGLELINE,
        );

        // Close button
        let _ = SelectObject(hdc, font_card_title);
        let _ = SetTextColor(hdc, COLORREF(COLOR_MUTED));
        draw_text_helper(
            hdc,
            "✕",
            RECT { left: total_w - 38, top: 14, right: total_w - 14, bottom: 38 },
            DT_CENTER | DT_VCENTER | DT_SINGLELINE,
        );

        let lock = POPUP_SNAPSHOT.lock().unwrap();
        let Some(ref snap) = *lock else {
            let _ = SelectObject(hdc, font_sub);
            let _ = SetTextColor(hdc, COLORREF(COLOR_MUTED));
            draw_text_helper(
                hdc,
                "Cargando cuotas...",
                RECT { left: 18, top: 60, right: total_w - 18, bottom: 90 },
                DT_LEFT | DT_SINGLELINE,
            );
            let _ = SelectObject(hdc, old_font);
            return;
        };

        let card_x = 14;
        let card_w = total_w - 28;
        let mut cur_y = 52;

        // 2. Antigravity Cards (Claude, Gemini, Argon)
        if let Some(ref a) = snap.antigravity.data {
            for g in &a.groups {
                let card_h = 62;
                draw_rounded_card(hdc, card_x, cur_y, card_w, card_h, COLOR_CARD);

                // Title: "🛸 AGY · {label}"
                let _ = SelectObject(hdc, font_card_title);
                let _ = SetTextColor(hdc, COLORREF(COLOR_TEXT));
                draw_text_helper(
                    hdc,
                    &format!("🛸 AGY · {}", g.label),
                    RECT { left: card_x + 12, top: cur_y + 8, right: card_x + card_w - 70, bottom: cur_y + 26 },
                    DT_LEFT | DT_VCENTER | DT_SINGLELINE,
                );

                // Pct
                let pct = g.remaining_5h_percent.or(g.weekly_remaining_percent);
                let _ = SelectObject(hdc, font_pct);
                let _ = SetTextColor(hdc, COLORREF(pct_color(pct)));
                let pct_text = pct.map(|p| format!("{:.0}%", p)).unwrap_or_else(|| "N/A".to_string());
                draw_text_helper(
                    hdc,
                    &pct_text,
                    RECT { left: card_x + card_w - 65, top: cur_y + 8, right: card_x + card_w - 12, bottom: cur_y + 26 },
                    DT_RIGHT | DT_VCENTER | DT_SINGLELINE,
                );

                // Progress bar
                draw_progress_bar(hdc, card_x + 12, cur_y + 30, card_w - 24, 6, pct);

                // Subtitle
                let mut sub_parts = Vec::new();
                if let Some(p5) = g.remaining_5h_percent {
                    let rst = g.reset_5h_in.as_deref().unwrap_or("N/A");
                    sub_parts.push(format!("5h {:.0}% (Reset: {})", p5, rst));
                }
                if let Some(pw) = g.weekly_remaining_percent {
                    let rst = g.reset_weekly_in.as_deref().unwrap_or("N/A");
                    sub_parts.push(format!("7d {:.0}% (Reset: {})", pw, rst));
                }
                let sub_text = sub_parts.join(" · ");
                let _ = SelectObject(hdc, font_sub);
                let _ = SetTextColor(hdc, COLORREF(COLOR_MUTED));
                draw_text_helper(
                    hdc,
                    &sub_text,
                    RECT { left: card_x + 12, top: cur_y + 40, right: card_x + card_w - 12, bottom: cur_y + 56 },
                    DT_LEFT | DT_VCENTER | DT_SINGLELINE,
                );

                cur_y += card_h + 8;
            }
        }

        // 3. Codex Cards (Codex, OpenCode)
        if let Some(ref c) = snap.codex.data {
            for acct in &c.accounts {
                let mut windows = Vec::new();
                if let Some(ref p) = acct.primary {
                    if let Some(rem) = p.remaining_percent {
                        let rst = p.resets_in.as_deref().unwrap_or("N/A");
                        windows.push(("5h", rem, rst));
                    }
                }
                if let Some(ref s) = acct.secondary {
                    if let Some(rem) = s.remaining_percent {
                        let rst = s.resets_in.as_deref().unwrap_or("N/A");
                        windows.push(("7d", rem, rst));
                    }
                }

                let has_resets = acct.reset_credits.is_some();
                let credits_extra = if has_resets { 18 } else { 0 };
                let card_h = 44 + (windows.len() as i32) * 30 + 20 + credits_extra;
                draw_rounded_card(hdc, card_x, cur_y, card_w, card_h, COLOR_CARD);

                // Title: "🔵 Codex · {label}"
                let _ = SelectObject(hdc, font_card_title);
                let _ = SetTextColor(hdc, COLORREF(COLOR_TEXT));
                draw_text_helper(
                    hdc,
                    &format!("🔵 Codex · {}", acct.label),
                    RECT { left: card_x + 12, top: cur_y + 8, right: card_x + card_w - 12, bottom: cur_y + 26 },
                    DT_LEFT | DT_VCENTER | DT_SINGLELINE,
                );

                let mut win_y = cur_y + 30;
                for (name, pct, _) in &windows {
                    // Window label "5h" / "7d"
                    let _ = SelectObject(hdc, font_win_label);
                    let _ = SetTextColor(hdc, COLORREF(COLOR_MUTED));
                    draw_text_helper(
                        hdc,
                        name,
                        RECT { left: card_x + 12, top: win_y + 2, right: card_x + 36, bottom: win_y + 22 },
                        DT_LEFT | DT_VCENTER | DT_SINGLELINE,
                    );

                    // Progress bar
                    draw_progress_bar(hdc, card_x + 40, win_y + 7, card_w - 110, 10, Some(*pct));

                    // Big percentage
                    let _ = SelectObject(hdc, font_pct_big);
                    let _ = SetTextColor(hdc, COLORREF(pct_color(Some(*pct))));
                    draw_text_helper(
                        hdc,
                        &format!("{:.0}%", pct),
                        RECT { left: card_x + card_w - 65, top: win_y - 2, right: card_x + card_w - 12, bottom: win_y + 22 },
                        DT_RIGHT | DT_VCENTER | DT_SINGLELINE,
                    );

                    win_y += 30;
                }

                // Subtitle
                let sub_text = if let Some(ref err) = acct.error {
                    format!("Cuota no disponible · {}", err)
                } else {
                    let plan = acct.plan_type.as_deref().unwrap_or("?").to_uppercase();
                    let resets = windows.iter().map(|(w, _, r)| format!("Reset {}: {}", w, r)).collect::<Vec<_>>().join(" · ");
                    format!("{} · {}", plan, resets)
                };

                let _ = SelectObject(hdc, font_sub);
                let _ = SetTextColor(hdc, COLORREF(COLOR_MUTED));
                draw_text_helper(
                    hdc,
                    &sub_text,
                    RECT { left: card_x + 12, top: win_y + 2, right: card_x + card_w - 12, bottom: win_y + 18 },
                    DT_LEFT | DT_VCENTER | DT_SINGLELINE,
                );

                // Reset credits
                if let Some(ref rc) = acct.reset_credits {
                    let avail = rc.available_count.unwrap_or(0);
                    let (credits_text, credits_color) = if avail > 0 {
                        (format!("🔄 {} resets disponibles", avail), COLOR_ACCENT)
                    } else {
                        ("🔄 Sin resets disponibles".to_string(), COLOR_MUTED)
                    };
                    let _ = SetTextColor(hdc, COLORREF(credits_color));
                    draw_text_helper(
                        hdc,
                        &credits_text,
                        RECT { left: card_x + 12, top: win_y + 18, right: card_x + card_w - 12, bottom: win_y + 34 },
                        DT_LEFT | DT_VCENTER | DT_SINGLELINE,
                    );
                }

                cur_y += card_h + 8;
            }
        }

        // 4. Z.AI Card
        if let Some(ref z) = snap.zai.data {
            let card_h = 62;
            draw_rounded_card(hdc, card_x, cur_y, card_w, card_h, COLOR_CARD);

            // Title: "🧠 Z.AI Coding Plan"
            let _ = SelectObject(hdc, font_card_title);
            let _ = SetTextColor(hdc, COLORREF(COLOR_TEXT));
            draw_text_helper(
                hdc,
                "🧠 Z.AI Coding Plan",
                RECT { left: card_x + 12, top: cur_y + 8, right: card_x + card_w - 70, bottom: cur_y + 26 },
                DT_LEFT | DT_VCENTER | DT_SINGLELINE,
            );

            let coding_lim = z.limits.iter().find(|l| l.kind == "coding_5h");
            let pct = coding_lim.map(|l| l.remaining_percent);

            // Pct
            let _ = SelectObject(hdc, font_pct);
            let _ = SetTextColor(hdc, COLORREF(pct_color(pct)));
            let pct_text = pct.map(|p| format!("{:.0}%", p)).unwrap_or_else(|| "N/A".to_string());
            draw_text_helper(
                hdc,
                &pct_text,
                RECT { left: card_x + card_w - 65, top: cur_y + 8, right: card_x + card_w - 12, bottom: cur_y + 26 },
                DT_RIGHT | DT_VCENTER | DT_SINGLELINE,
            );

            // Progress bar
            draw_progress_bar(hdc, card_x + 12, cur_y + 30, card_w - 24, 6, pct);

            // Subtitle: "LITE · Reset: 3h 10m"
            let level = z.level.as_deref().unwrap_or("?").to_uppercase();
            let rst = coding_lim.and_then(|l| l.reset_in.as_deref()).unwrap_or("N/A");
            let sub_text = format!("{} · Reset: {}", level, rst);
            let _ = SelectObject(hdc, font_sub);
            let _ = SetTextColor(hdc, COLORREF(COLOR_MUTED));
            draw_text_helper(
                hdc,
                &sub_text,
                RECT { left: card_x + 12, top: cur_y + 40, right: card_x + card_w - 12, bottom: cur_y + 56 },
                DT_LEFT | DT_VCENTER | DT_SINGLELINE,
            );
        }

        // Clean up GDI objects
        let _ = SelectObject(hdc, old_font);
        let _ = DeleteObject(font_title);
        let _ = DeleteObject(font_card_title);
        let _ = DeleteObject(font_pct);
        let _ = DeleteObject(font_pct_big);
        let _ = DeleteObject(font_sub);
        let _ = DeleteObject(font_win_label);
    }
}
