//! 尾部跟踪 lan-gateway 日志：app 生命周期内常驻一个线程，把新增行 emit 给前端，
//! 并从日志里识别「已代登录」→ 更新 session_acquired 标志。
use std::fs::OpenOptions;
use std::io::{BufRead, BufReader, Seek, SeekFrom};
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::thread;
use std::time::Duration;
use tauri::{AppHandle, Emitter};

static SESSION_ACQUIRED: AtomicBool = AtomicBool::new(false);

pub fn session_acquired() -> bool {
    SESSION_ACQUIRED.load(Ordering::Relaxed)
}

pub fn start(app: AppHandle, path: PathBuf) {
    thread::spawn(move || {
        // 等日志文件出现（转发没起之前不会有）
        let f = loop {
            match OpenOptions::new().read(true).open(&path) {
                Ok(f) => break f,
                Err(_) => thread::sleep(Duration::from_millis(500)),
            }
        };
        // 从末尾开始跟，只看新增行
        let mut f = f;
        let _ = f.seek(SeekFrom::End(0));
        let mut reader = BufReader::new(f);
        let mut buf = String::new();
        loop {
            buf.clear();
            match reader.read_line(&mut buf) {
                Ok(0) => thread::sleep(Duration::from_millis(250)),
                Ok(_) => {
                    let line = buf.trim_end_matches(['\n', '\r']).to_string();
                    if line.contains("已代登录") {
                        SESSION_ACQUIRED.store(true, Ordering::Relaxed);
                    }
                    let _ = app.emit("lan://log", line);
                }
                Err(_) => thread::sleep(Duration::from_millis(250)),
            }
        }
    });
}
