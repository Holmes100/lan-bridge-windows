# Windows 本地构建(从零到 .msi)

照这个走,一次就能出可双击安装的 msi。所有「装环境」都是一次性的,装完以后改代码 rebuild 只要重跑第 2 步。

---

## 第 0 步:把源码弄到 Windows

源码在这台 Linux 机:`/home/cpu/Project/lan-bridge-windows/`(或打包好的 `lan-bridge-windows.zip`)。
具体传输方式见对话(局域网 HTTP 下载 / scp / U盘 / git)。

---

## 第 1 步:装编译环境(一次性,约 20–40 分钟)

| 组件 | 装法 |
|---|---|
| **Visual Studio C++ Build Tools(MSVC)** | https://visualstudio.microsoft.com/visual-cpp-build-tools/ → 安装时勾 **「Desktop development with C++」**。Tauri Windows 必须有 MSVC。 |
| **Rust** | https://rustup.rs/ → 下 `rustup-init.exe` 一路默认(stable + msvc)。 |
| **Node.js LTS** | https://nodejs.org/ |
| **Python 3.10+** | https://www.python.org/ → 安装时**务必勾「Add python.exe to PATH」**。 |
| **WebView2 Runtime** | Win11 自带;Win10 从 https://developer.microsoft.com/microsoft-edge/webview2/ 装。 |
| **WiX v3**(msi 需要) | `npm run tauri build` 时 Tauri 会提示并指路;或手动 https://wixtoolset.org/releases/v3.14/ |

**装完新开一个 PowerShell 验证**:
```powershell
rustc --version ; cargo --version ; node --version ; python --version
```
四条都能出版本号才算齐。

---

## 第 2 步:build(约 5–10 分钟,首次更久)

```powershell
cd <你解压的路径>\lan-bridge-windows
npm install                 # 装 tauri CLI
.\build-python.bat          # PyInstaller 打 lan-gateway.exe
npm run tauri build         # 编译 + 打 msi
```

---

## 第 3 步:产物

```
src-tauri\target\release\bundle\msi\LAN Bridge (Windows)_0.1.0_x64_en-US.msi
```

双击装 → 开始菜单搜「**LAN Bridge (Windows)**」→ 打开就是 GUI。

---

## 第 4 步:常见报错对照

| 报错 | 原因 / 解决 |
|---|---|
| `'python' 不是内部命令` | Python 没加 PATH。重装勾选,或手动把 Python 目录加进系统 PATH。 |
| `link.exe` / `error: linker ... not found` | VS Build Tools 没装「C++」工具链。回第 1 步补装。 |
| `WiX` / `candle.exe` not found | WiX v3 没装。按 Tauri 提示装,或上面链接。 |
| 运行时 `ImportError: cryptography` | PyInstaller 没收集全 cryptography。用 `build-python.bat` 末尾那行带 `--collect-all cryptography` 重打。 |
| `error reading icons` | icon.ico 已在 `icons/` 里,正常不会报;若报,确认 `tauri.conf.json` 的 icon 数组路径。 |
| build 卡很久 | 首次要编译全部 Rust 依赖,正常。网慢的话 cargo 可能下依赖久。 |

---

## 调试(不想等打包)

```powershell
npm run tauri dev
```
直接弹出 GUI 窗口,用系统 `python` 跑 `lan-gateway-win.py`(不用先 `build-python.bat`),改代码热重载,适合先验证代登录/转发通不通。验证 OK 再 `npm run tauri build` 出正式 msi。

---

## build 之前的硬前提(WSL 侧)

msi 装好、打开 GUI 前,先在 Windows PowerShell 确认这四条(见 README §3):
```powershell
wsl -l -v
wsl bash -lc "which claude-science"
wsl bash -lc "claude-science url"   # 必须吐含 nonce= 的链接
curl http://127.0.0.1:8990          # localhost-forwarding 要通
```
这些不通,软件装上也用不起来(代登录/转发会失败)。
