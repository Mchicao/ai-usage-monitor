mod antigravity;
mod codex;
mod game_mode;
mod models;
mod tray;
mod zai;

use std::env;
use std::fs;
use std::path::PathBuf;
use std::time::Duration;

use chrono::Utc;
use models::AllQuotasSnapshot;
use tray_icon::menu::{Menu, MenuEvent, MenuItem};
use tray_icon::TrayIconBuilder;
use winit::application::ApplicationHandler;
use winit::event::WindowEvent;
use winit::event_loop::{ActiveEventLoop, ControlFlow, EventLoop};
use winit::window::WindowId;

fn snapshot_path() -> PathBuf {
    if let Ok(p) = env::var("AI_QUOTA_SNAPSHOT_FILE") {
        PathBuf::from(p)
    } else {
        PathBuf::from("..").join("quota_snapshot.json")
    }
}

pub async fn collect_all_quotas(client: &reqwest::Client) -> AllQuotasSnapshot {
    let ag_fut = antigravity::fetch_antigravity_quota(client);
    let codex_fut = codex::fetch_codex_quota(client);
    let zai_fut = zai::fetch_zai_quota(client);

    let (antigravity, codex, zai) = tokio::join!(ag_fut, codex_fut, zai_fut);

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

struct App {
    tray: Option<tray_icon::TrayIcon>,
    quit_id: tray_icon::menu::MenuId,
    open_dash_id: tray_icon::menu::MenuId,
    client: reqwest::Client,
    runtime: tokio::runtime::Handle,
    last_update: std::time::Instant,
}

impl ApplicationHandler for App {
    fn resumed(&mut self, _event_loop: &ActiveEventLoop) {
        if self.tray.is_none() {
            let menu = Menu::new();
            let open_dash = MenuItem::new("🌐 Abrir Dashboard Web", true, None);
            let quit_item = MenuItem::new("❌ Salir", true, None);

            self.open_dash_id = open_dash.id().clone();
            self.quit_id = quit_item.id().clone();

            let _ = menu.append(&open_dash);
            let _ = menu.append(&quit_item);

            let tray = TrayIconBuilder::new()
                .with_menu(Box::new(menu))
                .with_tooltip("AI Usage Monitor (Rust)")
                .build()
                .ok();

            self.tray = tray;

            // Trigger immediate collection
            self.update_quotas();
        }
    }

    fn window_event(&mut self, _event_loop: &ActiveEventLoop, _window_id: WindowId, _event: WindowEvent) {}

    fn about_to_wait(&mut self, event_loop: &ActiveEventLoop) {
        // Handle menu events
        if let Ok(event) = MenuEvent::receiver().try_recv() {
            if event.id == self.quit_id {
                event_loop.exit();
                return;
            } else if event.id == self.open_dash_id {
                let _ = std::process::Command::new("rundll32")
                    .args(["url.dll,FileProtocolHandler", "http://127.0.0.1:8420/"])
                    .spawn();
            }
        }

        // Periodic update every 120s
        if self.last_update.elapsed() >= Duration::from_secs(120) {
            self.update_quotas();
            self.last_update = std::time::Instant::now();
        }

        // Sleep to avoid busy loop
        std::thread::sleep(Duration::from_millis(100));
    }
}

impl App {
    fn update_quotas(&mut self) {
        // Game mode check
        if game_mode::is_game_or_fullscreen_active() {
            return;
        }

        let client = self.client.clone();
        let handle = self.runtime.clone();

        let snapshot = handle.block_on(async move {
            collect_all_quotas(&client).await
        });

        write_snapshot(&snapshot);

        if let Some(ref mut tray) = self.tray {
            if let Some(icon) = tray::create_tray_icon(&snapshot) {
                let _ = tray.set_icon(Some(icon));
            }
            let tooltip = tray::create_tooltip(&snapshot);
            let _ = tray.set_tooltip(Some(tooltip));
        }
    }
}

fn main() {
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
        let snapshot = rt.block_on(async {
            collect_all_quotas(&client).await
        });
        println!("{}", serde_json::to_string_pretty(&snapshot).unwrap());
        return;
    }

    // CLI mode: --print (Human-readable verification)
    if args.iter().any(|a| a == "--print") {
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
                println!(" - Account {}: primary={:?}%", a.label, a.primary.as_ref().and_then(|p| p.remaining_percent));
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

    // GUI / System Tray Loop
    let event_loop = EventLoop::new().expect("Failed to create event loop");
    event_loop.set_control_flow(ControlFlow::Poll);

    let mut app = App {
        tray: None,
        quit_id: tray_icon::menu::MenuId::new("quit"),
        open_dash_id: tray_icon::menu::MenuId::new("open_dash"),
        client,
        runtime: rt.handle().clone(),
        last_update: std::time::Instant::now(),
    };

    let _ = event_loop.run_app(&mut app);
}
