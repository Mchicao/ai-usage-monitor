#!/usr/bin/env python3
# pyright: basic, reportImplicitRelativeImport=false, reportMissingImports=false, reportMissingTypeArgument=false
"""
Widget de Cuota IA para Windows 11 — Versión CustomTkinter.
Diseño Fluent moderno con esquinas redondeadas, dark mode nativo.

Características:
  - Ícono nítido en system tray (3 barras de estado)
  - Click izquierdo → popup flotante con diseño Fluent (CustomTkinter)
  - Light dismiss (se cierra al perder foco)
  - Tooltip compacto optimizado (<127 chars para Win32)
"""

import http.server
import json
import os
import socketserver
import threading
import sys
import time

import customtkinter as ctk
import pystray
from ai_quota_widget import _ts_to_countdown, codex_windows_with_data, collect_all_quotas
from PIL import Image, ImageDraw

# ─── Configuración ───
ICON_UPDATE_INTERVAL = 120

# Tema Fluent
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

# Colores
BG_MAIN = "#1a1a1a"
BG_CARD = "#2b2b2b"
TEXT_PRIMARY = "#ffffff"
TEXT_MUTED = "#808080"
ACCENT = "#0078d4"

latest_data = {}
tray_icon = None
popup_window = None
tk_root = None
dashboard_server = None
dashboard_lock = threading.Lock()
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def fetch_quota_data() -> dict:
    """Consulta las cuotas dentro del mismo proceso para ahorrar RAM y CPU."""
    try:
        return collect_all_quotas()
    except Exception:
        pass
    return {}


# ═══════════════════════════════════════════════════════
#  ICONO DEL TRAY (32x32 nítido)
# ═══════════════════════════════════════════════════════


def create_icon_image(data: dict) -> Image.Image:
    size = 32
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    zai_pct = None
    if data.get("zai", {}).get("status") == "ok":
        for lim in data["zai"]["data"].get("limits", []):
            if lim.get("kind") == "coding_5h":
                zai_pct = lim.get("remaining_percent", 0)
                break

    codex_pct = None
    if data.get("codex", {}).get("status") == "ok":
        accounts = data["codex"]["data"].get("accounts") or [data["codex"]["data"]]
        pcts = []
        for acct in accounts:
            for _window_name, window in codex_windows_with_data(acct):
                pcts.append(window["remaining_percent"])
        if pcts:
            codex_pct = min(pcts)

    ag_pct = None
    if data.get("antigravity", {}).get("status") == "ok":
        groups = data["antigravity"]["data"].get("groups", [])
        pcts = []
        for group in groups:
            val = group.get("remaining_5h_percent")
            if not isinstance(val, (int, float)):
                val = group.get("weekly_remaining_percent")
            if isinstance(val, (int, float)):
                pcts.append(val)
        if pcts:
            ag_pct = min(pcts)

    bars = [ag_pct, codex_pct, zai_pct]
    bar_w = 6
    bar_h = 24
    start_x = 4
    gap = 9
    start_y = 4

    for i, pct in enumerate(bars):
        x = start_x + i * gap
        draw.rectangle([x, start_y, x + bar_w - 1, start_y + bar_h - 1], fill=(48, 54, 61, 255))
        if pct is not None:
            if pct > 50:
                color = (63, 185, 80, 255)
            elif pct > 20:
                color = (210, 153, 34, 255)
            else:
                color = (248, 81, 73, 255)
            fill_h = int(bar_h * pct / 100)
            if fill_h > 0:
                draw.rectangle(
                    [x, start_y + bar_h - fill_h, x + bar_w - 1, start_y + bar_h - 1], fill=color
                )

    return img


def get_compact_tooltip(data: dict) -> str:
    ag_t = "AG:N/A"
    cx_t = "CX:N/A"
    zi_t = "ZI:N/A"
    if data.get("antigravity", {}).get("status") in ("ok", "stale"):
        groups = data["antigravity"].get("data", {}).get("groups", [])
        pcts = []
        for group in groups:
            val = group.get("remaining_5h_percent")
            if not isinstance(val, (int, float)):
                val = group.get("weekly_remaining_percent")
            if isinstance(val, (int, float)):
                pcts.append((val, group))
        if pcts:
            min_val, min_group = min(pcts, key=lambda x: x[0])
            window = "5h" if isinstance(min_group.get("remaining_5h_percent"), (int, float)) else "weekly"
            r5h = min_group.get(f"reset_{window}_in") or _ts_to_countdown(min_group.get(f"reset_{window}_at"))
            if r5h and r5h not in ("N/A", "Ya reseteado"):
                ag_t = f"🛸AG:{min_val:.0f}%({r5h})"
            else:
                ag_t = f"🛸AG:{min_val:.0f}%"
    if data.get("codex", {}).get("status") == "ok":
        accounts = data["codex"]["data"].get("accounts") or [data["codex"]["data"]]
        parts = []
        for account in accounts:
            pcts = [window["remaining_percent"] for _, window in codex_windows_with_data(account)]
            if pcts:
                parts.append(f"{account.get('label', 'CX')[:2]}:{min(pcts):.0f}%")
        if parts:
            cx_t = " ".join(parts)
    if data.get("zai", {}).get("status") == "ok":
        for lim in data["zai"]["data"].get("limits", []):
            if lim.get("kind") == "coding_5h":
                zi_t = f"🧠ZI:{lim.get('remaining_percent', 0):.0f}%"
                break
    return f"{ag_t} | {cx_t} | {zi_t}\n(Click para detalles)"[:125]


# ═══════════════════════════════════════════════════════
#  POPUP FLOTANTE — CustomTkinter Fluent Design
# ═══════════════════════════════════════════════════════


class FluentPopup(ctk.CTkToplevel):
    """Ventana flotante Fluent con esquinas redondeadas, sin bordes."""

    def __init__(self, data: dict):
        super().__init__()
        self.data = data

        # Configurar ventana sin bordes, siempre al frente
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.configure(fg_color=BG_MAIN)

        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        try:
            import ctypes
            from ctypes import wintypes

            class _RECT(ctypes.Structure):
                _fields_ = [
                    ("left", ctypes.c_long),
                    ("top", ctypes.c_long),
                    ("right", ctypes.c_long),
                    ("bottom", ctypes.c_long),
                ]

            rect = _RECT()
            # SPI_GETWORKAREA = 0x0030
            if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):
                wa_w = rect.right - rect.left
                wa_h = rect.bottom - rect.top
                if wa_w > 0 and wa_h > 0:
                    sw, sh = wa_w, wa_h
        except (OSError, AttributeError):
            pass  # fallback a screenwidth/screenheight si Win32 falla

        self._screen_width = sw
        self._screen_height = sh
        self._build_ui()
        self._fit_to_content()

        # Light dismiss
        self.after(200, lambda: self.bind("<FocusOut>", lambda _event: close_popup()))
        self.bind("<Escape>", lambda _event: close_popup())

        # Forzar foco
        self.focus_force()

    def _fit_to_content(self):
        self.update_idletasks()
        height = max(420, min(self.winfo_reqheight(), self._screen_height - 40))
        self.geometry(f"350x{height}+{self._screen_width - 370}+{self._screen_height - height - 12}")

    def update_data(self, data: dict):
        self.data = data
        for child in self.winfo_children():
            child.destroy()
        self._build_ui()
        self._fit_to_content()

    def _build_ui(self):
        # Padding del frame principal
        self.grid_columnconfigure(0, weight=1)

        # ─── Header ───
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, padx=16, pady=(16, 8), sticky="ew")
        header.grid_columnconfigure(0, weight=1)

        title = ctk.CTkLabel(
            header,
            text="⚡ Cuotas IA",
            font=ctk.CTkFont(family="Segoe UI", size=16, weight="bold"),
            text_color=TEXT_PRIMARY,
        )
        title.grid(row=0, column=0, sticky="w")

        close_btn = ctk.CTkButton(
            header,
            text="✕",
            width=28,
            height=28,
            font=ctk.CTkFont(size=14),
            fg_color="transparent",
            hover_color="#3d3d3d",
            text_color=TEXT_MUTED,
            command=close_popup,
        )
        close_btn.grid(row=0, column=1)

        # ─── Cards de cada proveedor ───
        card_frame = ctk.CTkFrame(self, fg_color="transparent")
        card_frame.grid(row=1, column=0, padx=16, pady=(0, 16), sticky="nsew")
        card_frame.grid_columnconfigure(0, weight=1)

        # 1. Antigravity (aliases colapsados por familia/cuota compartida)
        row = 0
        for title, info in self._get_ag_groups():
            self._add_provider_card(card_frame, row, "🛸", f"AGY · {title}", info)
            row += 1
        # 2. Cuentas Codex — % grande del restante por ventana (5h / 7d)
        for title, info in self._get_cx_accounts():
            self._add_codex_card(card_frame, row, title, info)
            row += 1
        # 3. Z.AI
        self._add_provider_card(card_frame, row, "🧠", "Z.AI Coding Plan", self._get_zi_data())

    def _add_provider_card(self, parent, row, icon, title, info):
        """Agrega una tarjeta de proveedor con barra de progreso."""
        if not info:
            return

        card = ctk.CTkFrame(parent, fg_color=BG_CARD, corner_radius=8)
        card.grid(row=row, column=0, pady=4, sticky="ew")
        card.grid_columnconfigure(0, weight=1)

        # Fila superior: icono + título + porcentaje
        top_row = ctk.CTkFrame(card, fg_color="transparent")
        top_row.grid(row=0, column=0, padx=12, pady=(8, 2), sticky="ew")
        top_row.grid_columnconfigure(1, weight=1)

        icon_lbl = ctk.CTkLabel(top_row, text=icon, font=ctk.CTkFont(size=16))
        icon_lbl.grid(row=0, column=0, padx=(0, 6))

        name_lbl = ctk.CTkLabel(
            top_row,
            text=title,
            font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
            text_color=TEXT_PRIMARY,
            anchor="w",
        )
        name_lbl.grid(row=0, column=1, sticky="w")

        pct = info.get("pct")
        pct_text = f"{pct:.0f}%" if pct is not None else "N/A"
        pct_color = self._pct_color(pct)
        pct_lbl = ctk.CTkLabel(
            top_row,
            text=pct_text,
            font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold"),
            text_color=pct_color,
        )
        pct_lbl.grid(row=0, column=2)

        # Barra de progreso
        if pct is not None:
            bar = ctk.CTkProgressBar(
                card, height=6, corner_radius=3, progress_color=pct_color, fg_color="#3d3d3d"
            )
            bar.set(pct / 100.0)
            bar.grid(row=1, column=0, padx=12, pady=(0, 4), sticky="ew")

        # Subtítulo (detalles)
        subtitle = info.get("subtitle", "")
        if subtitle:
            sub_lbl = ctk.CTkLabel(
                card,
                text=subtitle,
                font=ctk.CTkFont(family="Segoe UI", size=10),
                text_color=TEXT_MUTED,
                anchor="w",
                justify="left",
                wraplength=310,
            )
            sub_lbl.grid(row=2, column=0, padx=12, pady=(0, 8), sticky="w")

    def _pct_color(self, pct):
        if pct is None:
            return TEXT_MUTED
        if pct > 50:
            return "#2ea043"
        if pct > 20:
            return "#d29922"
        return "#f85149"

    def _get_ag_groups(self):
        d = self.data.get("antigravity", {})
        if d.get("status") not in ("ok", "stale"):
            return []
        groups = d.get("data", {}).get("groups", [])
        stale_prefix = "Último dato conocido · " if d.get("data", {}).get("stale") else ""
        results = []
        for group in groups:
            five_hour = group.get("remaining_5h_percent")
            weekly = group.get("weekly_remaining_percent")
            pct = five_hour if isinstance(five_hour, (int, float)) else weekly
            windows = []
            if isinstance(five_hour, (int, float)):
                r5h = group.get("reset_5h_in") or _ts_to_countdown(group.get("reset_5h_at"))
                reset_text = f" (Reset: {r5h})" if r5h and r5h != "N/A" else ""
                windows.append(f"5h {five_hour:.0f}%{reset_text}")
            if isinstance(weekly, (int, float)):
                rw = group.get("reset_weekly_in") or _ts_to_countdown(group.get("reset_weekly_at"))
                reset_text = f" (Reset: {rw})" if rw and rw != "N/A" else ""
                windows.append(f"7d {weekly:.0f}%{reset_text}")
            results.append(
                (
                    group.get("label", "Modelos"),
                    {
                        "pct": pct,
                        "subtitle": f"{stale_prefix}{' · '.join(windows) or 'cuota no expuesta'}",
                    },
                )
            )
        return results

    def _get_cx_accounts(self):
        d = self.data.get("codex", {})
        if d.get("status") not in ("ok", "error") or not d.get("data"):
            return []
        accounts = d["data"].get("accounts") or [d["data"]]
        results = []
        for account in accounts:
            # Ventanas ordenadas de menor a mayor duración (5h antes que 7d),
            # etiquetadas por su duración real, no por posición.
            windows = sorted(
                codex_windows_with_data(account),
                key=lambda item: item[1].get("window_hours", 0),
            )
            plan = account.get("plan_type", "?").upper()
            if windows:
                resets = " · ".join(
                    f"Reset {name}: {window.get('resets_in', 'N/A')}" for name, window in windows
                )
                subtitle = f"{plan} · {resets}"
            else:
                subtitle = f"Cuota no disponible · {account['error']}" if account.get("status") == "error" else f"{plan} · sin ventanas expuestas"

            # Resets manuales de cuota (rate limit reset credits)
            reset_credits = account.get("reset_credits") or {}
            avail = reset_credits.get("available_count", 0)
            summary = reset_credits.get("summary")
            if avail > 0 and summary:
                resets_text = f"🔄 {avail} resets: {summary}"
            elif avail > 0:
                resets_text = f"🔄 {avail} resets disponibles"
            else:
                resets_text = "🔄 Sin resets disponibles"
            if account.get("status") == "error":
                resets_text = ""

            results.append(
                (
                    f"Codex · {account.get('label', 'Cuenta')}",
                    {
                        "windows": windows,
                        "subtitle": subtitle,
                        "resets_text": resets_text,
                        "resets_count": avail,
                    },
                )
            )
        return results

    def _add_codex_card(self, parent, row, title, info):
        """Tarjeta Codex: una fila por ventana (5h/7d) con barra y % grande del restante."""
        card = ctk.CTkFrame(parent, fg_color=BG_CARD, corner_radius=8)
        card.grid(row=row, column=0, pady=4, sticky="ew")
        card.grid_columnconfigure(0, weight=1)

        top_row = ctk.CTkFrame(card, fg_color="transparent")
        top_row.grid(row=0, column=0, padx=12, pady=(8, 4), sticky="ew")
        top_row.grid_columnconfigure(1, weight=1)

        icon_lbl = ctk.CTkLabel(top_row, text="🔵", font=ctk.CTkFont(size=16))
        icon_lbl.grid(row=0, column=0, padx=(0, 6))

        name_lbl = ctk.CTkLabel(
            top_row,
            text=title,
            font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
            text_color=TEXT_PRIMARY,
            anchor="w",
        )
        name_lbl.grid(row=0, column=1, sticky="w")

        windows = info.get("windows") or []
        for i, (name, window) in enumerate(windows):
            pct = window.get("remaining_percent")
            pct_color = self._pct_color(pct)
            wrow = ctk.CTkFrame(card, fg_color="transparent")
            wrow.grid(row=1 + i, column=0, padx=12, pady=2, sticky="ew")
            wrow.grid_columnconfigure(1, weight=1)

            win_lbl = ctk.CTkLabel(
                wrow,
                text=name,
                font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
                text_color=TEXT_MUTED,
            )
            win_lbl.grid(row=0, column=0, padx=(0, 8))

            bar = ctk.CTkProgressBar(
                wrow, height=10, corner_radius=5, progress_color=pct_color, fg_color="#3d3d3d"
            )
            bar.set((pct or 0) / 100.0)
            bar.grid(row=0, column=1, sticky="ew")

            pct_lbl = ctk.CTkLabel(
                wrow,
                text=f"{pct:.0f}%" if pct is not None else "N/A",
                width=64,
                font=ctk.CTkFont(family="Segoe UI", size=18, weight="bold"),
                text_color=pct_color,
            )
            pct_lbl.grid(row=0, column=2, padx=(8, 0))

        subtitle = info.get("subtitle", "")
        if subtitle:
            sub_lbl = ctk.CTkLabel(
                card,
                text=subtitle,
                font=ctk.CTkFont(family="Segoe UI", size=10),
                text_color=TEXT_MUTED,
                anchor="w",
            )
            sub_lbl.grid(row=1 + len(windows), column=0, padx=12, pady=(2, 2), sticky="w")

        resets_text = info.get("resets_text", "")
        if resets_text:
            resets_count = info.get("resets_count", 0)
            color = "#58a6ff" if resets_count > 0 else TEXT_MUTED
            resets_lbl = ctk.CTkLabel(
                card,
                text=resets_text,
                font=ctk.CTkFont(family="Segoe UI", size=10),
                text_color=color,
                anchor="w",
                justify="left",
                wraplength=310,
            )
            resets_lbl.grid(row=2 + len(windows), column=0, padx=12, pady=(0, 8), sticky="w")

    def _get_zi_data(self):
        d = self.data.get("zai", {})
        if d.get("status") != "ok":
            return None
        level = (d.get("data") or {}).get("level", "?").upper()
        for lim in (d.get("data") or {}).get("limits", []):
            if lim.get("kind") == "coding_5h":
                return {
                    "pct": lim.get("remaining_percent", 0),
                    "subtitle": f"{level} · Reset: {lim.get('reset_in', 'N/A')}",
                }
        return {"pct": None, "subtitle": level}

    def _open_web(self):
        close_popup()
        open_dashboard()


# ═══════════════════════════════════════════════════════
#  LOOP PRINCIPAL
# ═══════════════════════════════════════════════════════


class DashboardHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=SCRIPT_DIR, **kwargs)

    def do_GET(self):
        if self.path.startswith("/api/show") or self.path.startswith("/popup"):
            open_popup_window()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"OK")
            return
        if self.path.startswith("/api/quota"):
            body = json.dumps(latest_data, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path in ("/", "/dashboard"):
            self.path = "/dashboard.html"
        super().do_GET()

    def log_message(self, format: str, *args: object) -> None:
        pass


class DashboardServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def ensure_dashboard_server():
    global dashboard_server
    with dashboard_lock:
        if dashboard_server is None:
            dashboard_server = DashboardServer(("127.0.0.1", 8420), DashboardHandler)
            threading.Thread(target=dashboard_server.serve_forever, daemon=True).start()


def open_dashboard(*_args):
    ensure_dashboard_server()
    try:
        os.startfile("http://127.0.0.1:8420/")
    except OSError:
        pass


def close_popup():
    global popup_window
    window = popup_window
    popup_window = None
    if window:
        try:
            window.destroy()
        except (RuntimeError, AttributeError):
            pass


def _open_popup():
    global popup_window
    if popup_window and popup_window.winfo_exists():
        popup_window.lift()
        popup_window.focus_force()
        return
    popup_window = FluentPopup(latest_data.copy())


def open_popup_window():
    if tk_root:
        tk_root.after(0, _open_popup)


def _toggle_popup():
    global popup_window
    if popup_window and popup_window.winfo_exists():
        close_popup()
        return
    popup_window = FluentPopup(latest_data.copy())


def show_popup(_icon=None, _item=None):
    if tk_root:
        tk_root.after(0, _toggle_popup)


def refresh_open_popup():
    if popup_window and popup_window.winfo_exists():
        popup_window.update_data(latest_data.copy())


def refresh_loop(icon):
    global latest_data
    while True:
        latest_data = fetch_quota_data()
        if tk_root:
            try:
                tk_root.after(0, refresh_open_popup)
            except RuntimeError:
                pass
        if icon and latest_data:
            try:
                icon.icon = create_icon_image(latest_data)
                icon.title = get_compact_tooltip(latest_data)
            except (RuntimeError, OSError):
                pass
        time.sleep(ICON_UPDATE_INTERVAL)


def stop_app(icon=None, _item=None):
    if icon:
        icon.stop()
    if dashboard_server:
        dashboard_server.shutdown()
    if tk_root:
        tk_root.after(0, tk_root.destroy)


def _write_pid_lock():
    """Marca instancia viva para que el lanzador .vbs no duplique."""
    try:
        import os as _os

        lock = _os.path.join(SCRIPT_DIR, "win_tray_widget.lock")
        with open(lock, "w", encoding="utf-8") as _f:
            _f.write(str(_os.getpid()))
    except OSError:
        pass


def main():
    global tray_icon, latest_data, tk_root
    if "--popup" in sys.argv or "--show" in sys.argv:
        try:
            import urllib.request
            with urllib.request.urlopen("http://127.0.0.1:8420/popup", timeout=1) as resp:
                if resp.status == 200:
                    return
        except Exception:
            pass
    _write_pid_lock()
    latest_data = fetch_quota_data()
    tk_root = ctk.CTk()
    tk_root.withdraw()
    ensure_dashboard_server()

    tray_icon = pystray.Icon(
        "ai_usage_monitor",
        create_icon_image(latest_data),
        get_compact_tooltip(latest_data),
        menu=pystray.Menu(
            pystray.MenuItem("📊 Ver Detalle de Cuotas", show_popup, default=True),
            pystray.MenuItem("🌐 Abrir Dashboard Web", open_dashboard),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Salir", stop_app),
        ),
    )

    threading.Thread(target=refresh_loop, args=(tray_icon,), daemon=True).start()
    tray_icon.run_detached()
    if "--popup" in sys.argv or "--show" in sys.argv:
        tk_root.after(300, open_popup_window)
    try:
        tk_root.mainloop()
    finally:
        tray_icon.stop()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        with open(os.path.join(SCRIPT_DIR, "crash.log"), "w", encoding="utf-8") as f:
            traceback.print_exc(file=f)
