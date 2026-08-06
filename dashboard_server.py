#!/usr/bin/env python3
"""
Servidor web local para el Dashboard de Cuota IA.
Abre un navegador automáticamente con el dashboard visual.

Uso:
  python dashboard_server.py             # Puerto 8420
  python dashboard_server.py --port 8080 # Puerto personalizado
"""

import http.server
import json
import os
import sys
import subprocess
import threading
import time
import socketserver

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
WIDGET_SCRIPT = os.path.join(SCRIPT_DIR, "ai_quota_widget.py")

# Cache
_quota_cache = {"data": None, "timestamp": 0}
_cache_lock = threading.Lock()
CACHE_TTL = 120

# Python executable: prefer system python, fall back to current
SYS_PYTHON = r"C:\Program Files\Python310\python.exe"
if not os.path.exists(SYS_PYTHON):
    SYS_PYTHON = sys.executable


def fetch_quota_data() -> dict:
    """Ejecuta el script de cuota y devuelve el JSON."""
    global _quota_cache

    with _cache_lock:
        if _quota_cache["data"] and (time.time() - _quota_cache["timestamp"]) < CACHE_TTL:
            return _quota_cache["data"]

    try:
        result = subprocess.run(
            [SYS_PYTHON, WIDGET_SCRIPT, "--json"],
            capture_output=True,
            text=True,
            timeout=90,
            cwd=SCRIPT_DIR,
            encoding="utf-8",
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            with _cache_lock:
                _quota_cache["data"] = data
                _quota_cache["timestamp"] = time.time()
            return data
        else:
            return {
                "error": (result.stderr or result.stdout or "")[:500],
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
    except subprocess.TimeoutExpired:
        return {"error": "Timeout ejecutando script de cuota"}
    except Exception as e:
        return {"error": str(e)}


class DashboardHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=SCRIPT_DIR, **kwargs)

    def do_GET(self):
        if self.path.startswith("/api/quota"):
            self._handle_api()
        elif self.path in ("/", "/dashboard"):
            self.path = "/dashboard.html"
            super().do_GET()
        else:
            super().do_GET()

    def _handle_api(self):
        try:
            data = fetch_quota_data()
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            err = json.dumps({"error": str(e)}).encode()
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(err)))
            self.end_headers()
            self.wfile.write(err)

    def log_message(self, *args):
        pass  # Silenciar logs


class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    port = 8420
    if "--port" in sys.argv:
        idx = sys.argv.index("--port")
        if idx + 1 < len(sys.argv):
            port = int(sys.argv[idx + 1])

    # Pre-cargar datos en background
    t = threading.Thread(target=fetch_quota_data, daemon=True)
    t.start()

    server = ThreadedHTTPServer(("127.0.0.1", port), DashboardHandler)
    url = f"http://127.0.0.1:{port}/"

    # Abrir navegador en un thread separado
    def open_browser():
        time.sleep(1)
        try:
            os.startfile(url)
        except:
            pass
    threading.Thread(target=open_browser, daemon=True).start()

    print(f"Dashboard: {url}")
    print(f"API: {url}api/quota")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
