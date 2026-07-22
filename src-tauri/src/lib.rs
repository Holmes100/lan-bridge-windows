//! LAN Bridge —— 托管 lan-gateway.py，把本机 Claude Science 转发到局域网。
mod config;
mod gateway;
mod log_tail;

use std::sync::Mutex;
use tauri::{Manager, RunEvent};

/// 全局状态：当前托管的 lan-gateway.py 子进程（None = 未运行）。
pub struct AppState {
    child: Mutex<Option<std::process::Child>>,
}

pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _argv, _cwd| {
            // 已在跑就聚焦窗口
            if let Some(w) = app.get_webview_window("main") {
                let _ = w.show();
                let _ = w.set_focus();
            }
        }))
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .manage(AppState {
            child: Mutex::new(None),
        })
        .setup(|app| {
            // app 生命周期内常驻：尾部跟踪 lan-gateway 日志 → 推给前端
            let handle = app.handle().clone();
            log_tail::start(handle, config::log_path());
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            gateway::gateway_start,
            gateway::gateway_stop,
            gateway::gateway_status,
            gateway::get_lan_url,
            config::get_config,
            config::set_config,
        ])
        .build(tauri::generate_context!())
        .expect("error while building LAN Bridge")
        .run(|app, event| {
            // 退出时杀掉 python 子进程，防孤儿（std::process::Child 不会在 drop 时 kill）
            if let RunEvent::ExitRequested { .. } = event {
                if let Some(state) = app.try_state::<AppState>() {
                    let mut guard = state.child.lock().unwrap();
                    if let Some(child) = guard.as_mut() {
                        let _ = child.kill();
                        let _ = child.wait();
                    }
                    *guard = None;
                }
            }
        });
}
