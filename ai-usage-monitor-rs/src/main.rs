#![windows_subsystem = "windows"]

mod antigravity;
mod codex;
mod game_mode;
mod models;
mod popup;
mod tray;
mod zai;

use std::env;
use std::fs;
use std::io::Write;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

use chrono::Utc;
use models::AllQuotasSnapshot;
use tokio::sync::RwLock;
use windows::core::{w, PCWSTR};
use windows::Win32::Foundation::*;
use windows::Win32::Graphics::Gdi::HBRUSH;
use windows::Win32::System::LibraryLoader::GetModuleHandleW;
use windows::Win32::UI::Shell::*;
use windows::Win32::UI::WindowsAndMessaging::*;

const WM_TRAY: u32 = WM_USER + 20;
const WM_APPLY_SNAPSHOT: u32 = WM_USER + 21;
const ID_TRAY: u32 = 1001;

const IDM_SHOW_POPUP: usize = 2001;
const IDM_SYNC_NOW: usize = 2002;
const IDM_EXIT: usize = 2003;

static IS_UPDATING: AtomicBool = AtomicBool::new(false);

fn log_debug(msg: &str) {
    if let Ok(mut f) = fs::OpenOptions::new().create(true).append(true).open("C:\\Proyectos\\ai-usage-monitor\\debug.log") {
        let _ = writeln!(f, "[{}] [PID={}] {}", Utc::now().to_rfc3339(), std::process::id(), msg);
    }
}

fn snapshot_path() -> PathBuf {
    if let Ok(p) = env::var("AI_QUOTA_SNAPSHOT_FILE") {
        return PathBuf::from(p);
    }
    if PathBuf::from("quota_snapshot.json").exists() {
        return PathBuf::from("quota_snapshot.json");
    }
    if PathBuf::from("../quota_snapshot.json").exists() {
        return PathBuf::from("../quota_snapshot.json");
    }
    if let Ok(exe) = env::current_exe() {
        if let Some(parent) = exe.parent() {
            let candidate1 = parent.join("quota_snapshot.json");
            if candidate1.exists() {
                return candidate1;
            }
            let candidate2 = parent.join("../../../quota_snapshot.json");
            if candidate2.exists() {
                return candidate2;
            }
        }
    }
    PathBuf::from("quota_snapshot.json")
}

pub async fn collect_all_quotas(client: &reqwest::Client) -> AllQuotasSnapshot {
    log_debug("collect_all_quotas: starting futures");
    let ag_fut = async {
        log_debug("collect_all_quotas: fetching antigravity");
        let r = antigravity::fetch_antigravity_quota(client).await;
        log_debug("collect_all_quotas: antigravity done");
        r
    };

    let codex_fut = async {
        log_debug("collect_all_quotas: fetching codex");
        let r = codex::fetch_codex_quota(client).await;
        log_debug("collect_all_quotas: codex done");
        r
    };

    let zai_fut = async {
        log_debug("collect_all_quotas: fetching zai");
        let r = zai::fetch_zai_quota(client).await;
        log_debug("collect_all_quotas: zai done");
        r
    };

    let (antigravity, codex, zai) = tokio::join!(ag_fut, codex_fut, zai_fut);
    log_debug("collect_all_quotas: all 3 done!");

    AllQuotasSnapshot {
        antigravity,
        codex,
        zai,
        timestamp: Utc::now().to_rfc3339(),
    }
}

fn write_snapshot(snapshot: &AllQuotasSnapshot) {
    let path = snapshot_path();
    if let Ok(content) = serde_json::to_string_pretty(snapshot) {
        let temp = format!("{}.tmp", path.display());
        if fs::write(&temp, content).is_ok() {
            let _ = fs::rename(temp, path);
        }
    }
}

async fn run_dashboard_server(shared_snapshot: Arc<RwLock<Option<AllQuotasSnapshot>>>) {
    let listener = match tokio::net::TcpListener::bind("127.0.0.1:8420").await {
        Ok(l) => l,
        Err(_) => return,
    };

    let dashboard_html = include_str!("../../dashboard.html");

    loop {
        if let Ok((mut stream, _)) = listener.accept().await {
            let snap_ref = shared_snapshot.clone();
            tokio::spawn(async move {
                use tokio::io::{AsyncReadExt, AsyncWriteExt};
                let mut buf = [0u8; 2048];
                if let Ok(n) = stream.read(&mut buf).await {
                    let req = String::from_utf8_lossy(&buf[..n]);
                    if req.starts_with("GET /api/quota") {
                        let lock = snap_ref.read().await;
                        let body = if let Some(ref s) = *lock {
                            serde_json::to_string(s).unwrap_or_else(|_| "{}".into())
                        } else {
                            "{}".into()
                        };
                        let response = format!(
                            "HTTP/1.1 200 OK\r\nContent-Type: application/json; charset=utf-8\r\nAccess-Control-Allow-Origin: *\r\nCache-Control: no-store\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                            body.len(),
                            body
                        );
                        let _ = stream.write_all(response.as_bytes()).await;
                    } else if req.starts_with("GET /api/show") || req.starts_with("GET /popup") {
                        popup::show_popup();
                        let response = "HTTP/1.1 200 OK\r\nContent-Type: text/plain; charset=utf-8\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK";
                        let _ = stream.write_all(response.as_bytes()).await;
                    } else if req.starts_with("GET / ") || req.starts_with("GET /dashboard") {
                        let response = format!(
                            "HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                            dashboard_html.len(),
                            dashboard_html
                        );
                        let _ = stream.write_all(response.as_bytes()).await;
                    } else {
                        let not_found = "HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n";
                        let _ = stream.write_all(not_found.as_bytes()).await;
                    }
                }
            });
        }
    }
}

struct AppState {
    hwnd_usize: usize,
    current_hicon: Option<HICON>,
    client: reqwest::Client,
    rt_handle: tokio::runtime::Handle,
    shared_snapshot: Arc<RwLock<Option<AllQuotasSnapshot>>>,
}

static mut APP_STATE: Option<AppState> = None;

fn trigger_async_quota_collection() {
    log_debug("trigger_async_quota_collection called");
    if IS_UPDATING.swap(true, Ordering::SeqCst) {
        log_debug("Already updating, returning");
        return;
    }

    if game_mode::is_game_or_fullscreen_active() {
        log_debug("Game or fullscreen active, skipping update");
        IS_UPDATING.store(false, Ordering::SeqCst);
        return;
    }

    unsafe {
        let Some(ref state) = APP_STATE else {
            log_debug("APP_STATE is None, returning");
            IS_UPDATING.store(false, Ordering::SeqCst);
            return;
        };

        let client = state.client.clone();
        let hwnd_raw = state.hwnd_usize;

        log_debug("Spawning async quota collection task...");
        state.rt_handle.spawn(async move {
            log_debug("async task started, calling collect_all_quotas...");
            let snapshot = collect_all_quotas(&client).await;
            log_debug("collect_all_quotas finished, posting WM_APPLY_SNAPSHOT...");
            let boxed = Box::new(snapshot);
            let raw_ptr = Box::into_raw(boxed) as usize;
            let _ = PostMessageW(HWND(hwnd_raw as *mut _), WM_APPLY_SNAPSHOT, WPARAM(raw_ptr), LPARAM(0));
        });
    }
}

unsafe extern "system" fn wnd_proc(hwnd: HWND, msg: u32, wparam: WPARAM, lparam: LPARAM) -> LRESULT {
    unsafe {
        match msg {
            WM_APPLY_SNAPSHOT => {
                log_debug("WM_APPLY_SNAPSHOT received");
                let raw_ptr = wparam.0 as *mut AllQuotasSnapshot;
                if !raw_ptr.is_null() {
                    let snapshot = *Box::from_raw(raw_ptr);
                    IS_UPDATING.store(false, Ordering::SeqCst);

                    write_snapshot(&snapshot);
                    log_debug("Snapshot written to disk");

                    popup::update_popup_data(&snapshot);

                    if let Some(ref mut state) = APP_STATE {
                        let shared = state.shared_snapshot.clone();
                        let snap_copy = snapshot.clone();
                        state.rt_handle.spawn(async move {
                            let mut lock = shared.write().await;
                            *lock = Some(snap_copy);
                        });

                        if let Some(new_icon) = tray::create_tray_hicon(&snapshot) {
                            if let Some(old) = state.current_hicon.take() {
                                let _ = DestroyIcon(old);
                            }
                            state.current_hicon = Some(new_icon);

                            let tooltip = tray::create_tooltip(&snapshot);
                            let mut sz_tip: [u16; 128] = [0; 128];
                            let tip_utf16: Vec<u16> = tooltip.encode_utf16().collect();
                            for (i, c) in tip_utf16.iter().enumerate() {
                                if i < 127 {
                                    sz_tip[i] = *c;
                                }
                            }

                            let mut nid = NOTIFYICONDATAW {
                                cbSize: std::mem::size_of::<NOTIFYICONDATAW>() as u32,
                                hWnd: hwnd,
                                uID: ID_TRAY,
                                uFlags: NIF_ICON | NIF_TIP,
                                hIcon: new_icon,
                                szTip: sz_tip,
                                ..std::mem::zeroed()
                            };
                            let _ = Shell_NotifyIconW(NIM_MODIFY, &mut nid);
                            log_debug("Shell_NotifyIconW NIM_MODIFY called successfully");
                        }
                    }
                }
                LRESULT(0)
            }

            WM_TRAY => {
                let event = lparam.0 as u32;
                if event == WM_LBUTTONUP || event == WM_LBUTTONDBLCLK {
                    log_debug("WM_TRAY: toggle popup");
                    popup::toggle_popup();
                } else if event == WM_RBUTTONUP {
                    log_debug("WM_TRAY WM_RBUTTONUP: displaying context menu");
                    let mut cursor = POINT::default();
                    let _ = GetCursorPos(&mut cursor);

                    let hmenu = CreatePopupMenu().unwrap();
                    let _ = AppendMenuW(hmenu, MF_STRING, IDM_SHOW_POPUP, w!("📊 Ver Detalle de Cuotas"));
                    let _ = AppendMenuW(hmenu, MF_STRING, IDM_SYNC_NOW, w!("🔄 Sincronizar Ahora"));
                    let _ = AppendMenuW(hmenu, MF_SEPARATOR, 0, PCWSTR::null());
                    let _ = AppendMenuW(hmenu, MF_STRING, IDM_EXIT, w!("❌ Salir"));

                    let _ = SetForegroundWindow(hwnd);
                    let _ = TrackPopupMenuEx(
                        hmenu,
                        0,
                        cursor.x,
                        cursor.y,
                        hwnd,
                        None,
                    );
                    let _ = DestroyMenu(hmenu);
                }
                LRESULT(0)
            }

            WM_COMMAND => {
                let cmd = wparam.0 as usize;
                match cmd {
                    IDM_SHOW_POPUP => {
                        popup::show_popup();
                    }
                    IDM_SYNC_NOW => {
                        trigger_async_quota_collection();
                    }
                    IDM_EXIT => {
                        let _ = DestroyWindow(hwnd);
                    }
                    _ => {}
                }
                LRESULT(0)
            }

            WM_TIMER => {
                log_debug("WM_TIMER fired, triggering collection");
                trigger_async_quota_collection();
                LRESULT(0)
            }

            WM_DESTROY => {
                log_debug("WM_DESTROY received, cleaning up");
                popup::hide_popup();
                let mut nid = NOTIFYICONDATAW {
                    cbSize: std::mem::size_of::<NOTIFYICONDATAW>() as u32,
                    hWnd: hwnd,
                    uID: ID_TRAY,
                    ..std::mem::zeroed()
                };
                let _ = Shell_NotifyIconW(NIM_DELETE, &mut nid);

                if let Some(ref mut state) = APP_STATE {
                    if let Some(icon) = state.current_hicon.take() {
                        let _ = DestroyIcon(icon);
                    }
                }

                PostQuitMessage(0);
                LRESULT(0)
            }

            _ => DefWindowProcW(hwnd, msg, wparam, lparam),
        }
    }
}

fn attach_parent_console() {
    use std::fs::OpenOptions;
    use std::os::windows::io::IntoRawHandle;
    use windows::Win32::System::Console::{
        AttachConsole, SetStdHandle, ATTACH_PARENT_PROCESS, STD_ERROR_HANDLE, STD_OUTPUT_HANDLE,
    };

    unsafe {
        if AttachConsole(ATTACH_PARENT_PROCESS).is_ok() {
            if let Ok(conout) = OpenOptions::new().write(true).open("CONOUT$") {
                let raw = conout.into_raw_handle();
                let _ = SetStdHandle(STD_OUTPUT_HANDLE, windows::Win32::Foundation::HANDLE(raw as _));
                let _ = SetStdHandle(STD_ERROR_HANDLE, windows::Win32::Foundation::HANDLE(raw as _));
            }
        }
    }
}

fn main() {
    std::panic::set_hook(Box::new(|info| {
        log_debug(&format!("PANIC: {:?}", info));
    }));

    log_debug("main() entry");
    let args: Vec<String> = env::args().collect();

    let rt = tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()
        .expect("Failed to create tokio runtime");

    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(15))
        .build()
        .expect("Failed to build HTTP client");

    // CLI mode: --json
    if args.iter().any(|a| a == "--json") {
        attach_parent_console();
        let snapshot = rt.block_on(async {
            collect_all_quotas(&client).await
        });
        println!("{}", serde_json::to_string_pretty(&snapshot).unwrap());
        return;
    }

    // CLI mode: --print (Human-readable verification)
    if args.iter().any(|a| a == "--print") {
        attach_parent_console();
        let snapshot = rt.block_on(async {
            collect_all_quotas(&client).await
        });
        println!("=== ANTIGRAVITY ===");
        println!("Status: {}", snapshot.antigravity.status);
        if let Some(ref d) = snapshot.antigravity.data {
            for g in &d.groups {
                println!(" - {}: 5h={:?}% (reset: {:?})", g.label, g.remaining_5h_percent, g.reset_5h_in);
            }
        }
        println!("\n=== CODEX ===");
        println!("Status: {}", snapshot.codex.status);
        if let Some(ref d) = snapshot.codex.data {
            for a in &d.accounts {
                println!(" - Account {}: primary={:?}", a.label, a.primary.as_ref().and_then(|p| p.remaining_percent));
            }
        }
        println!("\n=== Z.AI ===");
        println!("Status: {}", snapshot.zai.status);
        if let Some(ref d) = snapshot.zai.data {
            for l in &d.limits {
                println!(" - {}: rem={}% (reset: {:?})", l.kind, l.remaining_percent, l.reset_in);
            }
        }
        return;
    }

    log_debug("Checking single-instance named mutex");
    let _mutex = unsafe {
        use windows::Win32::System::Threading::CreateMutexW;

        let m = CreateMutexW(None, true, w!("Local\\AIUsageMonitorRustMutex"));
        if GetLastError() == windows::Win32::Foundation::ERROR_ALREADY_EXISTS {
            log_debug("Another instance is already running according to mutex. Exiting.");
            if args.iter().any(|a| a == "--popup" || a == "--show") {
                let _ = std::process::Command::new("powershell")
                    .args(["-NoProfile", "-Command", "Invoke-RestMethod -Uri 'http://127.0.0.1:8420/popup' -TimeoutSec 1"])
                    .output();
            }
            return;
        }
        m
    };

    let shared_snapshot = Arc::new(RwLock::new(None));

    // Spawn internal HTTP server on 127.0.0.1:8420 for /api/quota and /popup
    rt.spawn(run_dashboard_server(shared_snapshot.clone()));
    log_debug("Dashboard server task spawned");

    // Initialize native popup window
    let _popup_hwnd = popup::init_popup();

    // Create native hidden window for tray icon
    unsafe {
        let hinstance = GetModuleHandleW(None).unwrap();

        let class_name: Vec<u16> = "ai_usage_monitor_tray_wnd\0".encode_utf16().collect();
        let wnd_class = WNDCLASSW {
            style: WNDCLASS_STYLES(0),
            lpfnWndProc: Some(wnd_proc),
            cbClsExtra: 0,
            cbWndExtra: 0,
            hInstance: hinstance.into(),
            hIcon: HICON::default(),
            hCursor: HCURSOR::default(),
            hbrBackground: HBRUSH::default(),
            lpszMenuName: PCWSTR::null(),
            lpszClassName: PCWSTR(class_name.as_ptr()),
        };

        let reg = RegisterClassW(&wnd_class);
        log_debug(&format!("RegisterClassW result: {}", reg));

        let hwnd = match CreateWindowExW(
            WINDOW_EX_STYLE(0),
            PCWSTR(class_name.as_ptr()),
            w!("AIUsageMonitorWindow"),
            WS_POPUP,
            0,
            0,
            0,
            0,
            None,
            None,
            hinstance,
            None,
        ) {
            Ok(h) => h,
            Err(e) => {
                log_debug(&format!("CreateWindowExW failed: {:?}", e));
                return;
            }
        };
        log_debug(&format!("CreateWindowExW succeeded: {:?}", hwnd));

        let initial_hicon = tray::create_default_icon();
        log_debug(&format!("initial_hicon: is_some={}", initial_hicon.is_some()));

        APP_STATE = Some(AppState {
            hwnd_usize: hwnd.0 as usize,
            current_hicon: initial_hicon,
            client,
            rt_handle: rt.handle().clone(),
            shared_snapshot,
        });

        let mut sz_tip: [u16; 128] = [0; 128];
        let tip: Vec<u16> = "AI Usage Monitor (Rust)\n(Click para detalles)\0".encode_utf16().collect();
        for (i, c) in tip.iter().enumerate() {
            if i < 127 {
                sz_tip[i] = *c;
            }
        }

        let mut nid = NOTIFYICONDATAW {
            cbSize: std::mem::size_of::<NOTIFYICONDATAW>() as u32,
            hWnd: hwnd,
            uID: ID_TRAY,
            uFlags: NIF_MESSAGE | NIF_ICON | NIF_TIP,
            uCallbackMessage: WM_TRAY,
            hIcon: initial_hicon.unwrap_or_default(),
            szTip: sz_tip,
            ..std::mem::zeroed()
        };

        let res = Shell_NotifyIconW(NIM_ADD, &mut nid);
        log_debug(&format!("Shell_NotifyIconW NIM_ADD result: {:?}", res));

        // Periodic timer: update quotas every 120 seconds
        SetTimer(hwnd, 1, 120_000, None);

        // Immediate first quota collection
        trigger_async_quota_collection();

        if args.iter().any(|a| a == "--popup" || a == "--show") {
            popup::show_popup();
        }

        log_debug("Entering native GetMessageW loop");
        let mut msg = MSG::default();
        while GetMessageW(&mut msg, None, 0, 0).into() {
            let _ = TranslateMessage(&msg);
            DispatchMessageW(&msg);
        }
        log_debug("Exited GetMessageW loop");
    }
}
