"""Lee la sesión Z.ai de ZCode sin modificar ni exportar credenciales."""

import base64
import ctypes
import hashlib
import json
import os
from pathlib import Path


class _AuthInfo(ctypes.Structure):
    _fields_ = [
        ("size", ctypes.c_uint32), ("version", ctypes.c_uint32),
        ("nonce", ctypes.c_void_p), ("nonce_size", ctypes.c_uint32),
        ("aad", ctypes.c_void_p), ("aad_size", ctypes.c_uint32),
        ("tag", ctypes.c_void_p), ("tag_size", ctypes.c_uint32),
        ("mac", ctypes.c_void_p), ("mac_size", ctypes.c_uint32),
        ("aad_count", ctypes.c_uint32), ("data_count", ctypes.c_uint64),
        ("flags", ctypes.c_uint32),
    ]


def decrypt_credential(value: str, secret: str) -> str:
    """AES-GCM autenticado con CNG de Windows; nunca devolver texto si falla el tag."""
    if not isinstance(value, str) or value.startswith("enc:") and not value.startswith("enc:v1:"):
        raise ValueError("Formato de credencial ZCode no compatible.")
    if not value.startswith("enc:v1:"):
        return value
    try:
        parts = value[7:].split(".")
        if len(parts) != 3:
            raise ValueError
        nonce, tag, cipher = (base64.b64decode(p + "=" * (-len(p) % 4), altchars=b"-_", validate=True) for p in parts)
        if len(nonce) != 12 or len(tag) != 16:
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError("Credencial ZCode cifrada no válida.") from None

    bcrypt = ctypes.WinDLL("bcrypt")
    pointer, uint = ctypes.c_void_p, ctypes.c_uint32
    signatures = {
        "BCryptOpenAlgorithmProvider": [pointer, ctypes.c_wchar_p, ctypes.c_wchar_p, uint],
        "BCryptSetProperty": [pointer, ctypes.c_wchar_p, pointer, uint, uint],
        "BCryptGenerateSymmetricKey": [pointer, pointer, pointer, uint, pointer, uint, uint],
        "BCryptDecrypt": [pointer, pointer, uint, pointer, pointer, uint, pointer, uint, pointer, uint],
        "BCryptDestroyKey": [pointer],
        "BCryptCloseAlgorithmProvider": [pointer, uint],
    }
    for name, args in signatures.items():
        function = getattr(bcrypt, name)
        function.argtypes, function.restype = args, ctypes.c_int32

    def check(status):
        if status < 0:
            raise ValueError("No se pudo autenticar la credencial ZCode.")

    algorithm, key = pointer(), pointer()
    try:
        check(bcrypt.BCryptOpenAlgorithmProvider(ctypes.byref(algorithm), "AES", None, 0))
        mode = ctypes.create_unicode_buffer("ChainingModeGCM")
        check(bcrypt.BCryptSetProperty(algorithm, "ChainingMode", mode, ctypes.sizeof(mode), 0))
        material = ctypes.create_string_buffer(hashlib.sha256(secret.encode()).digest(), 32)
        check(bcrypt.BCryptGenerateSymmetricKey(algorithm, ctypes.byref(key), None, 0, material, 32, 0))
        nonce_buffer, tag_buffer = ctypes.create_string_buffer(nonce), ctypes.create_string_buffer(tag)
        info = _AuthInfo(size=ctypes.sizeof(_AuthInfo), version=1,
                         nonce=ctypes.addressof(nonce_buffer), nonce_size=len(nonce),
                         tag=ctypes.addressof(tag_buffer), tag_size=len(tag))
        source = ctypes.create_string_buffer(cipher)
        output, written = ctypes.create_string_buffer(max(1, len(cipher))), uint()
        check(bcrypt.BCryptDecrypt(key, source, len(cipher), ctypes.byref(info), None, 0,
                                  output, len(output), ctypes.byref(written), 0))
        return output.raw[:written.value].decode("utf-8")
    finally:
        if key:
            bcrypt.BCryptDestroyKey(key)
        if algorithm:
            bcrypt.BCryptCloseAlgorithmProvider(algorithm, 0)


def read_zcode_session() -> dict | None:
    """Selecciona exclusivamente Z.ai; relee el almacén para seguir cambios de sesión."""
    home = Path(os.path.expanduser("~"))
    base = Path(os.environ.get("ZCODE_DATA_BASE_DIR") or os.environ.get("HOME") or home)
    path = base / ".zcode" / "v2" / "credentials.json"
    if not path.is_file():
        return None
    try:
        credentials = json.loads(path.read_text(encoding="utf-8"))
        username, size = ctypes.create_unicode_buffer(256), ctypes.c_uint32(256)
        if not ctypes.windll.advapi32.GetUserNameW(username, ctypes.byref(size)):
            raise ValueError
        secret = os.environ.get("ZCODE_CREDENTIAL_SECRET", "").strip() or f"zcode-credential-fallback:win32:{home}:{username.value}"

        def read(name):
            value = credentials.get(name)
            return decrypt_credential(value, secret).strip() if isinstance(value, str) else ""

        if read("oauth:active_provider") != "zai":
            raise ValueError("La familia activa de ZCode no es Z.ai.")
        settings = json.loads((path.parent / "setting.json").read_text(encoding="utf-8"))
        selection = (settings.get("providerFamilyConnectionSelections") or {}).get("zai") or {}
        if selection.get("kind") != "individual-coding-plan":
            raise ValueError("ZCode no tiene seleccionado el Coding Plan personal de Z.ai.")
        profile = json.loads(read("oauth:zai:user_info"))
        if not isinstance(profile, dict) or not (profile.get("user_id") or profile.get("id")):
            raise ValueError("Falta la identidad de Z.ai en ZCode.")
        token = read("oauth:zai:access_token")
        if not token:
            raise ValueError("Falta la sesión de plataforma Z.ai en ZCode.")
        try:
            zcode_token = read("zcodejwttoken")
        except ValueError:
            zcode_token = ""
        return {"platform_token": token, "zcode_token": zcode_token, "email": profile.get("email")}
    except (OSError, ValueError, TypeError, AttributeError):
        raise ValueError("No se pudo leer la sesión Z.ai de ZCode. Revisa su inicio de sesión.") from None
