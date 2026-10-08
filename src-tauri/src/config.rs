//! 配置：端口、沙箱 home、WSL 发行版、可选鉴权 token。
//! 持久化到 ~/.lan-bridge/config.json（unix 上 0600；Windows 上跳过权限设置）。
use serde::{Deserialize, Serialize};
use std::fs;
use std::path::PathBuf;

#[derive(Serialize, Deserialize, Clone)]
pub struct Config {
    pub sandbox_home: String,
    pub app_port: u16,
    pub content_port: u16,
    pub lan_port: u16,
    pub lan_content_port: u16,
    pub science_bin: String,
    /// 已废弃（原生模式不再使用）；保留字段以兼容旧 config.json，序列化时写空。
    #[serde(default = "default_wsl_distro")]
    pub wsl_distro: String,
    /// 空 = 不鉴权（默认）。非空 = 访问需带 ?token= / Bearer / cs_lan cookie。
    pub token: String,
    /// 启动 app 时自动拉起转发。
    pub autostart: bool,
}

fn default_wsl_distro() -> String {
    String::new()
}

fn home_dir() -> String {
    if let Ok(h) = std::env::var("HOME") {
        return h;
    }
    if let Ok(h) = std::env::var("USERPROFILE") {
        return h;
    }
    ".".to_string()
}

impl Default for Config {
    fn default() -> Self {
        Self {
            sandbox_home: String::new(),
            // Windows 原生 Claude Science 默认端口
            app_port: 8990,
            content_port: 8991,
            lan_port: 1450,
            lan_content_port: 1451,
            science_bin: String::new(),
            wsl_distro: default_wsl_distro(),
            token: String::new(),
            autostart: false,
        }
    }
}

pub fn data_dir() -> PathBuf {
    PathBuf::from(format!("{}/.lan-bridge", home_dir()))
}
pub fn config_path() -> PathBuf {
    data_dir().join("config.json")
}
pub fn log_path() -> PathBuf {
    data_dir().join("logs").join("lan-gateway.log")
}

impl Config {
    pub fn load() -> Self {
        match fs::read_to_string(config_path()) {
            Ok(s) => serde_json::from_str(&s).unwrap_or_default(),
            Err(_) => Config::default(),
        }
    }
    pub fn save(&self) -> Result<(), String> {
        let p = config_path();
        if let Some(parent) = p.parent() {
            fs::create_dir_all(parent).map_err(|e| e.to_string())?;
        }
        let s = serde_json::to_string_pretty(self).map_err(|e| e.to_string())?;
        fs::write(&p, &s).map_err(|e| e.to_string())?;
        // unix 上收紧权限（0600）；Windows 上无对应语义，跳过。
        #[cfg(target_family = "unix")]
        {
            use std::os::unix::fs::PermissionsExt;
            if let Ok(m) = fs::metadata(&p) {
                let mut perm = m.permissions();
                perm.set_mode(0o600);
                let _ = fs::set_permissions(&p, perm);
            }
        }
        Ok(())
    }
}

#[tauri::command]
pub fn get_config() -> Config {
    Config::load()
}

#[tauri::command]
pub fn set_config(cfg: Config) -> Result<Config, String> {
    cfg.save()?;
    Ok(cfg)
}
