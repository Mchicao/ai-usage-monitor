use std::{env, fs, path::PathBuf};

use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
use serde_json::Value;
use windows::{
    Win32::{Security::Cryptography::*, System::WindowsProgramming::GetUserNameW},
    core::{PCWSTR, PWSTR},
};

// Sin Debug: los tokens permanecen en memoria y no se incluyen en diagnósticos.
pub struct ZCodeSession {
    pub platform_token: String,
    pub zcode_token: Option<String>,
    pub email: Option<String>,
    pub identity: String,
}

fn decrypt(value: &str, secret: &str) -> Result<String, String> {
    if !value.starts_with("enc:") {
        return Ok(value.to_owned());
    }
    let payload = value
        .strip_prefix("enc:v1:")
        .ok_or("Formato de credencial ZCode no compatible.")?;
    let parts: Vec<_> = payload.split('.').collect();
    if parts.len() != 3 {
        return Err("Credencial ZCode cifrada no válida.".into());
    }
    let decode = |part| {
        URL_SAFE_NO_PAD
            .decode(part)
            .map_err(|_| "Credencial ZCode cifrada no válida.".to_owned())
    };
    let (mut nonce, mut tag, ciphertext) =
        (decode(parts[0])?, decode(parts[1])?, decode(parts[2])?);
    if nonce.len() != 12 || tag.len() != 16 {
        return Err("Credencial ZCode cifrada no válida.".into());
    }
    let mut material = [0u8; 32];
    let mut algorithm = BCRYPT_ALG_HANDLE::default();
    let mut key = BCRYPT_KEY_HANDLE::default();
    let result = unsafe {
        (|| -> windows::core::Result<String> {
            BCryptHash(
                BCRYPT_SHA256_ALG_HANDLE,
                None,
                secret.as_bytes(),
                &mut material,
            )
            .ok()?;
            BCryptOpenAlgorithmProvider(
                &mut algorithm,
                BCRYPT_AES_ALGORITHM,
                PCWSTR::null(),
                BCRYPT_OPEN_ALGORITHM_PROVIDER_FLAGS(0),
            )
            .ok()?;
            let mode: Vec<u8> = "ChainingModeGCM\0"
                .encode_utf16()
                .flat_map(u16::to_le_bytes)
                .collect();
            BCryptSetProperty(BCRYPT_HANDLE(algorithm.0), BCRYPT_CHAINING_MODE, &mode, 0).ok()?;
            BCryptGenerateSymmetricKey(algorithm, &mut key, None, &material, 0).ok()?;
            let info = BCRYPT_AUTHENTICATED_CIPHER_MODE_INFO {
                cbSize: std::mem::size_of::<BCRYPT_AUTHENTICATED_CIPHER_MODE_INFO>() as u32,
                dwInfoVersion: BCRYPT_AUTHENTICATED_CIPHER_MODE_INFO_VERSION,
                pbNonce: nonce.as_mut_ptr(),
                cbNonce: nonce.len() as u32,
                pbTag: tag.as_mut_ptr(),
                cbTag: tag.len() as u32,
                ..Default::default()
            };
            let mut plaintext = vec![0u8; ciphertext.len().max(1)];
            let mut written = 0;
            BCryptDecrypt(
                key,
                Some(&ciphertext),
                Some(&info as *const _ as _),
                None,
                Some(&mut plaintext),
                &mut written,
                BCRYPT_FLAGS(0),
            )
            .ok()?;
            plaintext.truncate(written as usize);
            String::from_utf8(plaintext).map_err(|_| {
                windows::core::Error::from_hresult(windows::core::HRESULT(0x8007000D_u32 as i32))
            })
        })()
    };
    unsafe {
        if !key.0.is_null() {
            let _ = BCryptDestroyKey(key);
        }
        if !algorithm.0.is_null() {
            let _ = BCryptCloseAlgorithmProvider(algorithm, 0);
        }
    }
    material.fill(0);
    result.map_err(|_| "No se pudo autenticar la credencial ZCode.".into())
}

pub fn read_session() -> Result<Option<ZCodeSession>, String> {
    let home = env::var("USERPROFILE")
        .map(PathBuf::from)
        .map_err(|_| "No se pudo resolver el usuario Windows.")?;
    let base = env::var("ZCODE_DATA_BASE_DIR")
        .ok()
        .filter(|v| !v.trim().is_empty())
        .or_else(|| env::var("HOME").ok().filter(|v| !v.trim().is_empty()))
        .map(PathBuf::from)
        .unwrap_or_else(|| home.clone());
    let content = match fs::read_to_string(base.join(".zcode/v2/credentials.json")) {
        Ok(content) => content,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(_) => return Err("No se pudo leer la sesión ZCode.".into()),
    };
    let credentials: Value =
        serde_json::from_str(&content).map_err(|_| "El almacén ZCode no es válido.")?;
    let secret = match env::var("ZCODE_CREDENTIAL_SECRET")
        .ok()
        .filter(|v| !v.trim().is_empty())
    {
        Some(secret) => secret.trim().to_owned(),
        None => {
            let mut username = [0u16; 256];
            let mut length = username.len() as u32;
            unsafe { GetUserNameW(PWSTR(username.as_mut_ptr()), &mut length) }
                .map_err(|_| "No se pudo resolver el usuario Windows.")?;
            let username = String::from_utf16_lossy(&username[..length.saturating_sub(1) as usize]);
            format!(
                "zcode-credential-fallback:win32:{}:{}",
                home.display(),
                username
            )
        }
    };
    let read = |name: &str| -> Result<String, String> {
        credentials
            .get(name)
            .and_then(Value::as_str)
            .map(|value| decrypt(value, &secret))
            .unwrap_or_else(|| Ok(String::new()))
    };
    if read("oauth:active_provider")? != "zai" {
        return Err("La familia activa de ZCode no es Z.ai.".into());
    }
    let settings = fs::read_to_string(base.join(".zcode/v2/setting.json"))
        .map_err(|_| "No se pudo confirmar el ámbito ZCode.")?;
    let settings: Value =
        serde_json::from_str(&settings).map_err(|_| "No se pudo confirmar el ámbito ZCode.")?;
    if settings
        .pointer("/providerFamilyConnectionSelections/zai/kind")
        .and_then(Value::as_str)
        != Some("individual-coding-plan")
    {
        return Err("ZCode no tiene seleccionado el Coding Plan personal de Z.ai.".into());
    }
    let profile: Value = serde_json::from_str(&read("oauth:zai:user_info")?)
        .map_err(|_| "Falta la identidad Z.ai en ZCode.")?;
    let identity = profile
        .get("user_id")
        .or_else(|| profile.get("id"))
        .and_then(Value::as_str)
        .filter(|id| !id.is_empty())
        .ok_or("Falta la identidad Z.ai en ZCode.")?
        .to_owned();
    let platform_token = read("oauth:zai:access_token")?;
    if platform_token.trim().is_empty() {
        return Err("Falta la sesión de plataforma Z.ai en ZCode.".into());
    }
    let zcode_token = read("zcodejwttoken").ok().filter(|v| !v.trim().is_empty());
    Ok(Some(ZCodeSession {
        platform_token,
        zcode_token,
        email: profile
            .get("email")
            .and_then(Value::as_str)
            .map(str::to_owned),
        identity,
    }))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn decrypt_zcode_fixture_and_reject_wrong_key() {
        let encrypted = "enc:v1:AAECAwQFBgcICQoL.pDgAVwUYxIHJXs-yqAhSqA.dnlWI6JDR2z3DbvZRUPvvAjhiA";
        assert_eq!(
            decrypt(encrypted, "monitor-test-secret").unwrap(),
            "fake-platform-token"
        );
        assert!(decrypt(encrypted, "wrong-secret").is_err());
    }
}
