#!/usr/bin/env python3
"""
Extrae cookies de Z.AI desde TODOS los navegadores Chromium instalados.
Busca en Chrome, Edge, Thorium, Antigravity, Hermes, etc.
"""
import os, sys, sqlite3, json, base64, shutil, tempfile, glob

# Todos los navegadores Chromium conocidos en el sistema
CHROMIUM_PROFILES = [
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\User Data\Default\Network\Cookies"),
    os.path.expandvars(r"%LOCALAPPDATA%\Thorium\User Data\Default\Network\Cookies"),
    os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\User Data\Default\Network\Cookies"),
    os.path.expandvars(r"%LOCALAPPDATA%\imput\Helium\User Data\Default\Network\Cookies"),
    os.path.expandvars(r"%LOCALAPPDATA%\com.nousresearch.hermes.setup\EBWebView\Default\Network\Cookies"),
    os.path.expandvars(r"%LOCALAPPDATA%\com.antigravity-agent.app\EBWebView\Default\Network\Cookies"),
]

# También buscar perfiles adicionales (Profile 1, Profile 2, etc.)
for browser_base in [
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\User Data"),
    os.path.expandvars(r"%LOCALAPPDATA%\Thorium\User Data"),
]:
    if os.path.isdir(browser_base):
        for profile in os.listdir(browser_base):
            if profile.startswith("Profile") or profile == "Default":
                p = os.path.join(browser_base, profile, "Network", "Cookies")
                if p not in CHROMIUM_PROFILES:
                    CHROMIUM_PROFILES.append(p)


def try_decrypt_chromium(encrypted_value, local_state_path):
    """Desencripta cookies de Chromium (v10/v20 AES-GCM con DPAPI)."""
    if not encrypted_value:
        return None

    # Valor plano (no encriptado)
    if encrypted_value[:3] not in (b'v10', b'v20'):
        return encrypted_value.decode('utf-8', errors='ignore')

    try:
        with open(local_state_path, 'r') as f:
            local_state = json.load(f)
        master_key_b64 = local_state.get("os_crypt", {}).get("encrypted_key", "")
        if not master_key_b64:
            return None

        master_key = base64.b64decode(master_key_b64)
        if master_key[:5] == b'DPAPI':
            master_key = master_key[5:]

        import win32crypt
        _, master_key = win32crypt.CryptUnprotectData(master_key, None, None, None, 0)

        nonce = encrypted_value[3:15]
        ciphertext_and_tag = encrypted_value[15:]
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        cipher = AESGCM(master_key)
        decrypted = cipher.decrypt(nonce, ciphertext_and_tag, None)
        return decrypted.decode('utf-8', errors='replace')
    except Exception as e:
        # Fallback: DPAPI directo
        try:
            import win32crypt
            result = win32crypt.CryptUnprotectData(encrypted_value, None, None, None, 0)
            return result[1].decode('utf-8', errors='ignore')
        except:
            return None


def find_local_state(cookie_path):
    """Encuentra el archivo Local State correspondiente al perfil de cookies."""
    parts = cookie_path.replace("\\", "/").split("/")
    # Buscar "User Data" o el root del navegador
    for i, part in enumerate(parts):
        if part in ("User Data", "EBWebView", "EBWebView"):
            return os.path.join("/".join(parts[:i+1]), "Local State")
        if part == "Default" or part.startswith("Profile"):
            # El Local State está un nivel arriba
            return os.path.join("/".join(parts[:i]), "Local State")
    return None


def scan_all_browsers():
    """Escanea todos los navegadores buscando cookies de z.ai."""
    results = {}

    for cookie_path in CHROMIUM_PROFILES:
        if not os.path.exists(cookie_path):
            continue

        browser_name = "Unknown"
        lower = cookie_path.lower()
        if "chrome" in lower: browser_name = "Chrome"
        elif "thorium" in lower: browser_name = "Thorium"
        elif "edge" in lower: browser_name = "Edge"
        elif "helium" in lower: browser_name = "Helium"
        elif "hermes" in lower: browser_name = "Hermes"
        elif "antigravity" in lower: browser_name = "Antigravity"

        # Detectar perfil
        profile = "Default"
        if "Profile" in cookie_path:
            for part in cookie_path.replace("\\", "/").split("/"):
                if part.startswith("Profile"):
                    profile = part

        label = f"{browser_name} ({profile})"
        print(f"  🔍 {label}...")

        # Copiar BD a temp
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            shutil.copy2(cookie_path, tmp_path)
        except:
            print(f"     ⚠️  No se pudo leer (navegador abierto?)")
            continue

        # Buscar Local State para desencriptar
        # Recorrer hacia arriba buscando "User Data" o similar
        local_state_path = None
        normalized = cookie_path.replace("/", "\\")
        parts = normalized.split("\\")
        for i in range(len(parts) - 1, 0, -1):
            candidate_dir = "\\".join(parts[:i])
            candidate_file = os.path.join(candidate_dir, "Local State")
            if os.path.exists(candidate_file):
                local_state_path = candidate_file
                break

        if not local_state_path:
            # Intentar patrón genérico: buscar "Local State" 2 niveles arriba de Network
            base = os.path.dirname(os.path.dirname(cookie_path))  # subir de Network/
            test = os.path.join(base, "Local State")
            if os.path.exists(test):
                local_state_path = test
            else:
                # subir un nivel más (de Default/)
                base2 = os.path.dirname(base)
                test2 = os.path.join(base2, "Local State")
                if os.path.exists(test2):
                    local_state_path = test2

        try:
            conn = sqlite3.connect(tmp_path)
            cur = conn.cursor()
            cur.execute(
                "SELECT host_key, name, encrypted_value, value FROM cookies WHERE host_key LIKE '%z.ai%'"
            )
            rows = cur.fetchall()
            conn.close()

            if not rows:
                continue

            print(f"     ✅ {len(rows)} cookies de z.ai encontradas!")

            cookies = {}
            for host, name, enc_val, plain_val in rows:
                val = plain_val
                if not val and enc_val and local_state_path:
                    val = try_decrypt_chromium(enc_val, local_state_path)
                if val:
                    cookies[name] = val

            if cookies:
                results[label] = {
                    "cookies": cookies,
                    "source": cookie_path,
                }
                print(f"     📋 {len(cookies)} cookies desencriptadas")
        except Exception as e:
            print(f"     ❌ Error: {e}")
        finally:
            try:
                os.unlink(tmp_path)
            except:
                pass

    return results


if __name__ == "__main__":
    print("🔍 Buscando cookies de Z.AI en todos los navegadores...\n")

    results = scan_all_browsers()

    if not results:
        print("\n❌ No se encontraron cookies de z.ai en ningún navegador.")
        print("   Asegúrate de haber visitado z.ai y haber iniciado sesión.")
        sys.exit(1)

    # Usar el primer navegador con cookies
    for label, data in results.items():
        cookies = data["cookies"]
        print(f"\n{'='*60}")
        print(f"📋 {label} — {len(cookies)} cookies:")
        for name, val in cookies.items():
            display = val[:40] + "..." if len(val) > 40 else val
            print(f"   {name}: {display}")

        # Guardar para el scraper
        session_file = os.path.expanduser("~/AppData/Local/hermes/.zai_session.json")
        with open(session_file, 'w') as f:
            json.dump(cookies, f)
        print(f"\n💾 Cookies guardadas en: {session_file}")
        break
