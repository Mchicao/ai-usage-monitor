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
import time

import customtkinter as ctk
import pystray
from ai_quota_widget import collect_all_quotas
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
            val = (acct.get("primary") or {}).get("remaining_percent")
            if isinstance(val, (int, float)):
                pcts.append(val)
        if pcts:
            codex_pct = min(pcts)

    ag_pct = None
    if data.get("antigravity", {}).get("status") == "ok":
        groups = data["antigravity"]["data"].get("groups", [])
        pcts = []
        for group in groups:
            val = group.get("remaining_5h_percent")
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
    if data.get("antigravity", {}).get("status") == "ok":
        groups = data["antigravity"]["data"].get("groups", [])
        pcts = []
        for group in groups:
            val = group.get("remaining_5h_percent")
            if isinstance(val, (int, float)):
                pcts.append(val)
        if pcts:
            ag_t = f"🛸AG:{min(pcts):.0f}%"
    if data.get("codex", {}).get("status") == "ok":
        accounts = data["codex"]["data"].get("accounts") or [data["codex"]["data"]]
        parts = []
        for account in accounts:
            val = (account.get("primary") or {}).get("remaining_percent")
            if isinstance(val, (int, float)):
                parts.append(f"{account.get('label', 'CX')[:2]}:{val:.0f}%")
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

        # Dimensiones y posición (esquina inferior derecha del área de trabajo).
        # La altura debe incluir todas las tarjetas activas: un alto fijo de 480px
        # cortaba Z.AI y el footer cuando había varios modelos/cuentas.
        w = 340
        card_count = (
            len(self._get_ag_groups())
            + len(self._get_cx_accounts())
            + (1 if self._get_zi_data() else 0)
        )
        h = max(480, 140 + card_count * 90)
        # Usar el área de trabajo real (excluye la barra de tareas) vía Win32,
        # en lugar de winfo_screenheight() que devuelve el alto físico total.
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
        x = sw - w - 20
        y = sh - h - 12  # 12px de margen inferior sobre el área de trabajo
        self.geometry(f"{w}x{h}+{x}+{y}")

        # Light dismiss
        self.after(200, lambda: self.bind("<FocusOut>", lambda _event: close_popup()))
        self.bind("<Escape>", lambda _event: close_popup())

        self._build_ui()

        # Forzar foco
        self.focus_force()

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
        card_frame.grid(row=1, column=0, padx=16, pady=(0, 8), sticky="nsew")
        card_frame.grid_columnconfigure(0, weight=1)

        # 1. Antigravity (aliases colapsados por familia/cuota compartida)
        row = 0
        for title, info in self._get_ag_groups():
            self._add_provider_card(card_frame, row, "🛸", f"AGY · {title}", info)
            row += 1
        # 2. Cuentas Codex
        for title, info in self._get_cx_accounts():
            self._add_provider_card(card_frame, row, "🔵", title, info)
            row += 1
        # 3. Z.AI
        self._add_provider_card(card_frame, row, "🧠", "Z.AI Coding Plan", self._get_zi_data())

        # ─── Footer ───
        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.grid(row=2, column=0, padx=16, pady=(0, 16), sticky="ew")

        web_btn = ctk.CTkButton(
            footer,
            text="🌐 Abrir Dashboard Web",
            font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
            fg_color=ACCENT,
            hover_color="#106ebe",
            height=32,
            corner_radius=6,
            command=self._open_web,
        )
        web_btn.pack(fill="x")

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
        if d.get("status") != "ok":
            return []
        groups = d.get("data", {}).get("groups", [])
        stale_prefix = "Último dato conocido · " if d.get("data", {}).get("stale") else ""
        return [
            (
                group.get("label", "Modelos"),
                {
                    "pct": group.get("remaining_5h_percent"),
                    "subtitle": (
                        f"{stale_prefix}5h · {group.get('model_count', 0)} aliases · semanal no expuesta"
                    ),
                },
            )
            for group in groups
        ]

    def _get_cx_accounts(self):
        d = self.data.get("codex", {})
        if d.get("status") != "ok":
            return []
        accounts = d["data"].get("accounts") or [d["data"]]
        results = []
        for account in accounts:
            primary = account.get("primary") or {}
            secondary = account.get("secondary") or {}
            pct = primary.get("remaining_percent")
            sec_pct = secondary.get("remaining_percent")
            sec_text = f"7d {sec_pct:.0f}%" if isinstance(sec_pct, (int, float)) else "7d N/A"
            prim_window = primary.get("window_hours", 5)
            if prim_window <= 5:
                prim_label = "5h"
            elif prim_window >= 168:
                prim_label = "7d"
            else:
                prim_label = f"{prim_window:.0f}h"
            results.append(
                (
                    f"Codex · {account.get('label', 'Cuenta')}",
                    {
                        "pct": pct,
                        "subtitle": (
                            f"{account.get('plan_type', '?').upper()}"
                            f" · {prim_label} {primary.get('resets_in', 'N/A')}"
                            f" · {sec_text}"
                        ),
                    },
                )
            )
        return results

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


def _toggle_popup():
    global popup_window
    if popup_window and popup_window.winfo_exists():
        close_popup()
        return
    popup_window = FluentPopup(latest_data.copy())


def show_popup(_icon=None, _item=None):
    if tk_root:
        tk_root.after(0, _toggle_popup)


def refresh_loop(icon):
    global latest_data
    while True:
        latest_data = fetch_quota_data()
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
    try:
        tk_root.mainloop()
    finally:
        tray_icon.stop()


if __name__ == "__main__":
    main()
