//! lan-gateway 子进程管理：spawn / stop / status，以及资源解析、python 解析、
//! LAN IP 探测、TCP 探活。
//! Windows 版：打包后 spawn resource 里的 lan-gateway.exe；dev 模式用 python 跑 .py。
use crate::config::Config;
use crate::AppState;
use serde_json::{json, Value};
use std::net::{SocketAddr, TcpStream, UdpSocket};
use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::time::Duration;
use tauri::{AppHandle, Manager, State};

#[tauri::command]
pub fn gateway_start(app: AppHandle, state: State<'_, AppState>) -> Result<Value, String> {
    // 先判断是否已在跑（PID 存活）；死了就收尸
    {
        let mut guard = state.child.lock().unwrap();
        if let Some(child) = guard.as_mut() {
            if matches!(child.try_wait(), Ok(None)) {
                return Ok(json!({"already_running": true, "lan_url": lan_url()}));
            }
            let _ = child.wait();
            *guard = None;
        }
    }

    let cfg = Config::load();

    // 确保日志目录存在（脚本也会建，这里先建好让 tail 少等）
    let log_path = crate::config::log_path();
    if let Some(parent) = log_path.parent() {
        let _ = std::fs::create_dir_all(parent);
    }

    // 打包版：直接 spawn resource 里的 lan-gateway.exe。
    // dev 版：用 python 跑项目根的 lan-gateway-win.py。
    let (program, args): (String, Vec<String>) = if let Some(exe) = exe_path(&app) {
        (exe, vec![])
    } else {
        let py = python().ok_or_else(|| {
            "找不到 python：dev 模式需要装 Python；打包前需要先跑 build-python.bat 生成 lan-gateway.exe".to_string()
        })?;
        let script = dev_script_path().ok_or_else(|| "找不到 lan-gateway-win.py".to_string())?;
        (py, vec![script])
    };

    let mut cmd = Command::new(&program);
    for a in &args {
        cmd.arg(a);
    }
    cmd.stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .env("CS_APP_PORT", cfg.app_port.to_string())
        .env("CS_CONTENT_PORT", cfg.content_port.to_string())
        .env("CS_LAN_PORT", cfg.lan_port.to_string())
        .env("CS_LAN_CONTENT_PORT", cfg.lan_content_port.to_string())
        .env("CS_SCIENCE_BIN", &cfg.science_bin)
        .env("CS_LAN_LOG", &log_path);
    // token 空 = 不鉴权（默认内网可信）；非空才开 guard
    if !cfg.token.is_empty() {
        cmd.env("CS_GUARD_TOKEN", &cfg.token);
    }

    // Windows：CREATE_NO_WINDOW，不弹控制台黑框（PyInstaller exe 默认带控制台窗口）
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x0800_0000);
    }

    let child = cmd.spawn().map_err(|e| format!("启动 lan-gateway 失败：{e}"))?;
    *state.child.lock().unwrap() = Some(child);
    Ok(json!({ "lan_url": lan_url_with(&cfg) }))
}

#[tauri::command]
pub fn gateway_stop(state: State<'_, AppState>) -> Result<(), String> {
    let mut guard = state.child.lock().unwrap();
    if let Some(child) = guard.as_mut() {
        // Windows：PyInstaller onefile 是 bootloader + python 子进程，taskkill /T 杀整棵树，
        // 避免只 kill 主进程后 python 子进程残留（下次启动又弹新黑框）。
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            let _ = std::process::Command::new("taskkill")
                .args(["/F", "/T", "/PID", &child.id().to_string()])
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .creation_flags(0x0800_0000)
                .status();
        }
        let _ = child.kill();
        let _ = child.wait();
    }
    *guard = None;
    Ok(())
}

#[tauri::command]
pub fn gateway_status(state: State<'_, AppState>) -> Value {
    let cfg = Config::load();
    let running = {
        let mut guard = state.child.lock().unwrap();
        match guard.as_mut() {
            Some(child) => match child.try_wait() {
                Ok(None) => true,
                _ => {
                    let _ = child.wait();
                    *guard = None;
                    false
                }
            },
            None => false,
        }
    };
    json!({
        "running": running,
        "listening": running && listening_on(cfg.lan_port),
        "content_listening": listening_on(cfg.lan_content_port),
        "session_acquired": crate::log_tail::session_acquired(),
        "lan_url": lan_url_with(&cfg),
        "lan_port": cfg.lan_port,
    })
}

#[tauri::command]
pub fn get_lan_url() -> Value {
    json!({ "lan_url": lan_url() })
}

// ---------- helpers ----------

/// 打包后的 lan-gateway.exe 路径。
/// 优先 resource_dir/scripts/；某些 nsis 安装下 resource_dir() 不指向安装目录，
/// 再用「本体 exe 同目录 / scripts/」兜底（lan-gateway.exe 和本体在同一安装目录的 scripts/ 下）。
fn exe_path(app: &AppHandle) -> Option<String> {
    if let Ok(res) = app.path().resource_dir() {
        let p = res.join("scripts").join("lan-gateway.exe");
        if p.is_file() {
            return Some(p.to_string_lossy().to_string());
        }
    }
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            let p = dir.join("scripts").join("lan-gateway.exe");
            if p.is_file() {
                return Some(p.to_string_lossy().to_string());
            }
        }
    }
    None
}

/// dev 模式脚本：CARGO_MANIFEST_DIR = .../src-tauri，向上一级即项目根。
fn dev_script_path() -> Option<String> {
    let dev = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../lan-gateway-win.py");
    if dev.is_file() {
        Some(dev.to_string_lossy().to_string())
    } else {
        None
    }
}

/// 找 python 解释器（仅 dev 模式用）。Windows 上 python/python3/py 都试。
fn python() -> Option<String> {
    for c in ["python", "python3", "py"] {
        if which(c) {
            return Some(c.to_string());
        }
    }
    None
}

fn which(cmd: &str) -> bool {
    // Windows 上可执行文件通常带 .exe；cmd 本身或 cmd.exe 命中都算找到。
    let candidates: Vec<String> = if cfg!(windows) && !cmd.ends_with(".exe") {
        vec![cmd.to_string(), format!("{}.exe", cmd)]
    } else {
        vec![cmd.to_string()]
    };
    for c in &candidates {
        if std::path::Path::new(c).is_file() {
            return true;
        }
        if let Some(path) = std::env::var_os("PATH") {
            for dir in std::env::split_paths(&path) {
                if dir.join(c).is_file() {
                    return true;
                }
            }
        }
    }
    false
}

/// UDP connect 8.8.8.8 的 trick 探默认出口 IP（不发包），拿本机 LAN 地址。
fn lan_ip() -> Option<String> {
    let s = UdpSocket::bind("0.0.0.0:0").ok()?;
    s.connect("8.8.8.8:80").ok()?;
    let a = s.local_addr().ok()?;
    if a.ip().is_loopback() {
        None
    } else {
        Some(a.ip().to_string())
    }
}

fn lan_url() -> String {
    lan_url_with(&Config::load())
}

fn lan_url_with(cfg: &Config) -> String {
    let host = lan_ip().unwrap_or_else(|| "<本机IP>".to_string());
    if cfg.token.is_empty() {
        format!("https://{}:{}/", host, cfg.lan_port)
    } else {
        format!("https://{}:{}/?token={}", host, cfg.lan_port, cfg.token)
    }
}

fn listening_on(port: u16) -> bool {
    let addr: SocketAddr = match format!("127.0.0.1:{}", port).parse() {
        Ok(a) => a,
        Err(_) => return false,
    };
    TcpStream::connect_timeout(&addr, Duration::from_millis(600)).is_ok()
}
