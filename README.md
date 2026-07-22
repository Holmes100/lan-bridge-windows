# LAN Bridge (Windows)

把 **WSL2 里的 Claude Science** 经自签 HTTPS + WebSocket 转发给局域网的 **Windows 原生 GUI 应用**（Tauri v2 + WebView2，产物 `.msi`）。

转发逻辑跑在 **Windows host** 上，监听 `0.0.0.0:1450/1451`；上游 Science 实际在 WSL2，靠 WSL2 的 localhost-forwarding 让 Windows host 的 `127.0.0.1:8990/8991` 可达。代登录时通过 `wsl.exe` 调进 WSL 跑 `claude-science url` 拿 nonce。

> 这是 [`lan-bridge`](../lan-bridge)（Linux 版）的 Windows 对应版本，**独立目录、独立 identifier**，互不干扰。核心转发逻辑（WS 代理、代登录 cookie 注入、响应改写）与 Linux 版一致，仅适配了 Windows/WSL 的差异。

---

## 1. 为什么有它

Claude Science 在 Windows 上**只能跑在 WSL2 里**（官方文档：Windows 原生不支持 sandboxing）。如果你想在 Windows 上把本机（WSL 里）的 Science 暴露给局域网其它机器免登录访问，就需要这个 Windows 版转发器。

---

## 2. 架构

```
┌──────────────────────────────────────────────────────────────┐
│  Windows host                                                │
│  LAN Bridge.exe (Tauri: Rust 后端 + WebView2 前端)           │
│    gateway_start ──spawn──▶ lan-gateway.exe  (PyInstaller)   │
│       (Stdio::null + env)        │  自签 HTTPS 1450→8990      │
│                                  │  WS         1450→8990      │
│    log_tail 线程 ◀──尾部跟踪 ~/.lan-bridge/logs/...          │
│         └─emit("lan://log")──▶ 前端实时日志面板              │
│    代登录：wsl.exe bash -lc "HOME=<沙箱> claude-science url" │
└────────────────────────────┬─────────────────────────────────┘
                             │ 127.0.0.1:8990/8991
                             │ (WSL2 localhost-forwarding)
┌────────────────────────────▼─────────────────────────────────┐
│  WSL2：claude-science（Science 后端，8990/8991）             │
└──────────────────────────────────────────────────────────────┘
```

---

## 3. ⚠️ 先验证 WSL 前提（在 Windows 上）

```powershell
wsl -l -v                            # 1) 看发行版名 → 填 config 的「WSL 发行版名」
wsl bash -lc "which claude-science"   # 2) 看 claude-science 路径 → 填「claude-science 路径」
wsl bash -lc "claude-science url"     # 3) 确认能输出含 nonce= 的 http 链接
# WSL 里把 Science 跑起来后，Windows PowerShell 里：
curl http://127.0.0.1:8990            # 4) 确认 localhost-forwarding 通（非 connection refused）
```

- 第 4 步若不通：检查 `%USERPROFILE%\.wslconfig` 是否设了 `localhostForwarding=false`（**默认是 true**）。
- `wsl bash -lc "claude-science url"` 必须能吐出 nonce 链接，否则代登录起不来。

---

## 4. 构建（在 Windows 上）

**前置**：Rust（stable）、Node 18+、Python 3.10+、WebView2 Runtime（Win11 自带）、WiX v3（打 msi 需要，`npm run tauri build` 时 Tauri 会提示安装）。

```powershell
cd <path>\lan-bridge-windows
npm install                 # 装 tauri CLI
.\build-python.bat          # PyInstaller 打包 lan-gateway-win.py → lan-gateway.exe
npm run tauri build         # 产 msi（首次 cargo 编依赖约 5min）
```

产物：
```
src-tauri\target\release\bundle\msi\LAN Bridge (Windows)_0.1.0_x64_en-US.msi
```

安装该 msi → 开始菜单「LAN Bridge (Windows)」→ 填设置（WSL 发行版名、沙箱 HOME、claude-science 路径）→ 点「启动转发」。

> **调试**：`npm run tauri dev` 可直接跑（dev 模式用系统 `python` 跑 `lan-gateway-win.py`，**无需先 `build-python.bat`**），便于快速验证代登录/转发。

---

## 5. 配置（`%USERPROFILE%\.lan-bridge\config.json`）

| 字段 | 默认 | 含义 |
|---|---|---|
| `wsl_distro` | `Ubuntu` | WSL 发行版名（空=wsl.exe 默认） |
| `sandbox_home` | *(空，必填)* | **WSL 内的 Linux 路径**，如 `/home/you/.csswitch/sandbox/home` |
| `science_bin` | `~/.local/bin/claude-science` | **WSL 内**的 claude-science 路径（`~` 由 WSL 的 bash 展开） |
| `app_port` / `content_port` | 8990 / 8991 | Science 在 WSL 里监听的端口（不变） |
| `lan_port` / `lan_content_port` | 1450 / 1451 | Windows host 暴露给局域网的端口 |
| `token` | *(空)* | 空=不鉴权；非空=访问需带 `?token=` / `Bearer` / `cs_lan` cookie |
| `autostart` | false | 打开 GUI 即自动开转发 |

> **关键**：`sandbox_home` 和 `science_bin` 填的是 **WSL 里的 Linux 路径**，不是 Windows 路径。代登录是 `wsl.exe` 进 WSL 里执行，认的是 Linux 路径。

---

## 6. 与 Linux 版（`lan-bridge`）的差异

| 项 | Linux 版 | Windows 版 |
|---|---|---|
| GUI 后端 | WebKitGTK | WebView2 |
| 转发脚本运行处 | Linux host | **Windows host** |
| 代登录拿 nonce | 直接 `claude-science url` | `wsl.exe bash -lc "... claude-science url"` |
| 自签证书 | `openssl` CLI | Python `cryptography` 库（Windows 无 openssl） |
| Python 分发 | 要求系统 `python3` | PyInstaller 打 `lan-gateway.exe` 随包走（免装） |
| 产物 | deb / appimage | msi |
| identifier | `com.lanbridge.app` | `com.lanbridge.app.windows` |
| keepalive | `TCP_KEEPIDLE/INTVL/CNT` | `SIO_KEEPALIVE_VALS` ioctl |
| 转发/WS/鉴权/重写核心 | — | **完全一致** |

---

## 7. 已知坑

- **Windows 防火墙**：首次「启动转发」会弹窗要求放行 `lan-gateway.exe` 监听 1450/1451，需点「允许」（专用网络/所有网络看你的局域网信任度）。
- **端口冲突**：同一台 Windows 上不能同时跑别的绑 1450 的东西。
- **WSL2 localhost-forwarding**：默认开。若你在 `.wslconfig` 关过，Windows host 访问不到 WSL 的 8990/8991，转发会全 502。
- **Science 没起**：转发启动后代登录会失败（日志「启动代登录失败（WSL/Science 可能还没起）」）。先把 Science 在 WSL 里跑起来。
- **PyInstaller + cryptography**：极少数情况下运行时报 `ImportError`（cryptography 子模块没被收集），用 `build-python.bat` 末尾提示的 `--collect-all cryptography` 重新打包。

---

## 8. 不在范围内（同 Linux 版）

- provider/profile 切换、沙箱启停——仍归 CSSwitch 管。
- claude-science 内部 DB 卡顿——Science 二进制内部的事，转发侧只能看到 502。
