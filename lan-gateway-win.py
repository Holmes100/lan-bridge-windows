#!/usr/bin/env python3
"""
LAN Bridge (Windows) —— 把 WSL2 里的 Claude Science 经自签 HTTPS + WebSocket
转发给局域网。本脚本跑在 Windows host 上(Python 或 PyInstaller 打的 exe)。

监听两个 LAN 端口，反代到 Science（实际在 WSL2，经 WSL localhost-forwarding 可达）：
    0.0.0.0:{CS_LAN_PORT}(默认1450)  ->  127.0.0.1:{CS_APP_PORT}(默认8990, Science app)
    0.0.0.0:{CS_LAN_CONTENT_PORT}(默认1451) -> 127.0.0.1:{CS_CONTENT_PORT}(默认8991, sandbox content)

自动代登录：启动/上游 401 时，通过 wsl.exe 在 WSL 里跑 `claude-science url`
（沙箱 HOME）拿 nonce，再在 Windows host 这边 GET /?nonce + POST /api/auth/nonce
取得会话 cookie，注入到所有转发请求，这样远程浏览器不用登录 Science。
（拿 nonce 要跨界进 WSL；后续登录请求仍走 Windows host 的 127.0.0.1:899x，靠
 WSL2 的 localhost-forwarding 默认转发。）

自鉴权：访问需带 ?token= 查询参数 或 Authorization: Bearer 或 cs_lan cookie（== CS_GUARD_TOKEN）。
首次用 ?token= 访问会下发 cs_lan cookie，浏览器记住，之后链接里不带 token 也行。

依赖：除标准库外只用 cryptography（生成自签证书，Windows 上无 openssl）。
所有参数走环境变量（secret 不进 argv）。
"""
import os
import sys
import time
import shlex
import socket
import ssl
import threading
import subprocess
import urllib.request
import urllib.parse
import urllib.error
import http.cookiejar
import http.server
import socketserver
import secrets
import datetime
import ipaddress

# ---- 配置（环境变量）----
SANDBOX_HOME = os.environ.get("CS_SANDBOX_HOME", "")
APP_PORT = int(os.environ.get("CS_APP_PORT", "8990"))
CONTENT_PORT = int(os.environ.get("CS_CONTENT_PORT", "8991"))
LAN_PORT = int(os.environ.get("CS_LAN_PORT", "1450"))
LAN_CONTENT_PORT = int(os.environ.get("CS_LAN_CONTENT_PORT", "1451"))
GUARD_TOKEN = os.environ.get("CS_GUARD_TOKEN", "")
SCIENCE_BIN = os.environ.get("CS_SCIENCE_BIN", "claude-science")
# WSL 发行版名（空 = wsl.exe 默认）。claude-science 跑在 WSL 里，代登录要跨界进去。
WSL_DISTRO = os.environ.get("CS_WSL_DISTRO", "")
# 自开日志文件：desktop 把 stdout/stderr 接到了空设备（见 gateway.rs 的 Stdio::null()），
# 不写文件就没法自查重连/异常。
LOG_PATH = os.environ.get("CS_LAN_LOG") or os.path.expanduser("~/.lan-bridge/logs/lan-gateway.log")

CONNECT_TIMEOUT = 10
# 转发请求时跳过的 hop-by-hop / 自管头
HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
    "cookie",  # 由 session jar 统一注入
    "authorization",  # 客户端的 Bearer 是网关 guard token，不是 Science 的；Science 认 cookie
    "accept-encoding",  # 不让上游 gzip/br，否则文本改写会损坏压缩体 -> ERR_CONTENT_DECODING_FAILED
}

# ---- HTTPS：自签证书（Science 前端用 crypto.randomUUID/subtle，需安全上下文）----
CERT_PATH = os.environ.get("CS_CERT") or os.path.expanduser("~/.lan-bridge/lan-gateway-cert.pem")
KEY_PATH = os.environ.get("CS_KEY") or os.path.expanduser("~/.lan-bridge/lan-gateway-key.pem")


def all_lan_ips():
    """收集本机非 loopback 的 IPv4（写进证书 SAN，减少名称不匹配提示）。
    不用 `hostname -I`（Linux 专属）；改用 getaddrinfo + UDP connect trick。"""
    ips = []
    try:
        hn = socket.gethostname()
        for info in socket.getaddrinfo(hn, None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except Exception:
        pass
    # UDP connect trick：不发包，拿默认出口网卡 IP（与 Rust 侧 lan_ip 一致）
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            if not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
        finally:
            s.close()
    except Exception:
        pass
    return ips or ["127.0.0.1"]


def ensure_cert():
    """没有就生成自签证书（含本机 IP 的 SAN）。已存在则复用。
    Windows 上没有 openssl，用 cryptography 库生成（纯 Python wheel，无外部依赖）。"""
    if os.path.exists(CERT_PATH) and os.path.exists(KEY_PATH):
        return
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    ips = all_lan_ips()
    os.makedirs(os.path.dirname(CERT_PATH) or ".", exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "lan-bridge")])
    san = [x509.DNSName("localhost"), x509.DNSName("csswitch")]
    for ip in ips:
        try:
            san.append(x509.IPAddress(ipaddress.ip_address(ip)))
        except ValueError:
            pass  # 非法 IP 串，跳过
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName(san), critical=False)
        .sign(key, hashes.SHA256())
    )
    with open(KEY_PATH, "wb") as f:
        f.write(key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ))
    with open(CERT_PATH, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    log(f"已生成自签证书：{CERT_PATH}（SAN: {','.join(ips)}）")


def make_ssl_context():
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(CERT_PATH, KEY_PATH)
    return ctx


# ---- 会话 cookie（代登录态）----
SESSION_LOCK = threading.Lock()
SESSION_JAR: http.cookiejar.CookieJar | None = None


def log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [lan-gateway] {msg}", flush=True)


def _redirect_stdio_to_log() -> None:
    """desktop 启动本进程时把 stdout/stderr 都接到空设备（见 gateway.rs 的
    Stdio::null()），重连/异常/代登录日志全丢。这里重定向到 LOG_PATH，方便自查。
    失败则静默退回原 stdio（不影响转发功能）。"""
    try:
        os.makedirs(os.path.dirname(LOG_PATH) or ".", exist_ok=True)
        f = open(LOG_PATH, "a", encoding="utf-8", buffering=1)  # 行缓冲，近实时
        sys.stdout = f
        sys.stderr = f
    except Exception:
        pass


def _wsl_argv(linux_cmd: str):
    """构造 wsl.exe 调用：在（指定发行版的）WSL 里跑 `bash -lc <linux_cmd>`。"""
    argv = ["wsl.exe"]
    if WSL_DISTRO:
        argv += ["-d", WSL_DISTRO]
    argv += ["bash", "-lc", linux_cmd]
    return argv


def acquire_session() -> http.cookiejar.CookieJar:
    """跑一遍 nonce 流程，返回含 Science 会话 cookie 的 CookieJar。
    第 1 步（拿 nonce 链接）跨界进 WSL 调 claude-science；后续登录请求仍在
    Windows host 访问 127.0.0.1:899x（靠 WSL2 localhost-forwarding）。"""
    if not SANDBOX_HOME:
        raise RuntimeError("CS_SANDBOX_HOME 未设置，无法代登录")
    if not WSL_DISTRO:
        log("⚠️ CS_WSL_DISTRO 未设置，使用 wsl.exe 默认发行版")
    # 1. claude-science url 拿 nonce 链接（在 WSL 里，HOME 设为沙箱 home）
    linux_cmd = f"HOME={shlex.quote(SANDBOX_HOME)} {shlex.quote(SCIENCE_BIN)} url"
    # Windows 中文系统默认用 GBK 解码，但 wsl.exe 透传的是 Linux 的 UTF-8 输出
    # （claude-science url 的框线字符/中文），GBK 解不了会抛 UnicodeDecodeError 让代登录崩。
    # 强制 UTF-8 + errors=replace，绝不在解码上挂掉。
    proc = subprocess.run(
        _wsl_argv(linux_cmd), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=30,
    )
    nonce_url = None
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if line.startswith("http://") and "nonce=" in line:
            nonce_url = line.split()[0]
            break
    if not nonce_url:
        raise RuntimeError(f"claude-science url 未给出 nonce 链接：{proc.stderr or proc.stdout}")
    parsed = urllib.parse.urlparse(nonce_url)
    q = urllib.parse.parse_qs(parsed.query)
    nonce = q.get("nonce", [None])[0]
    if not nonce:
        raise RuntimeError("nonce 链接里没有 nonce 参数")
    # nonce_url 通常是 localhost:port（claude-science url 输出）。统一改成 127.0.0.1:APP_PORT，
    # 保证 GET 拿 cookie 与 POST 用同一 host → cookie 同 domain → POST 能带上 operon_csrf；
    # 否则 GET 在 localhost 拿的 cookie，POST 到 127.0.0.1 不带 → Science 判 CSRF 失败 → 401。
    nonce_url = parsed._replace(netloc=f"127.0.0.1:{APP_PORT}").geturl()
    # 2. GET /?nonce -> operon_csrf cookie（Windows host 经 WSL forwarding 访问 8000/8001）
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    opener.open(nonce_url, timeout=CONNECT_TIMEOUT).read()
    # 3. POST /api/auth/nonce -> 会话 cookie
    data = urllib.parse.urlencode({"nonce": nonce, "dest": "/"}).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{APP_PORT}/api/auth/nonce", data=data, method="POST",
    )
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        opener.open(req, timeout=CONNECT_TIMEOUT).read()
    except urllib.error.HTTPError as e:
        body = e.read()[:300].decode("utf-8", "replace")
        log(f"POST /api/auth/nonce 失败：HTTP {e.code}；已带 cookie={[c.name for c in jar]}；响应={body}")
        raise
    return jar


def get_session() -> http.cookiejar.CookieJar:
    """取当前会话 jar，没有则代登录。线程安全。"""
    global SESSION_JAR
    with SESSION_LOCK:
        if SESSION_JAR is None:
            SESSION_JAR = acquire_session()
            log(f"已代登录，会话 cookie 数={len(SESSION_JAR)}")
        return SESSION_JAR


def refresh_session() -> http.cookiejar.CookieJar:
    """强制重新代登录（上游 401 时）。"""
    global SESSION_JAR
    with SESSION_LOCK:
        SESSION_JAR = acquire_session()
        log(f"已重新代登录，会话 cookie 数={len(SESSION_JAR)}")
        return SESSION_JAR


def cookie_header_for(jar: http.cookiejar.CookieJar) -> str:
    cookies = []
    for c in jar:
        cookies.append(f"{c.name}={c.value}")
    return "; ".join(cookies)


def session_operon_csrf():
    """SESSION_JAR 里的 operon_csrf 值（下发给浏览器，让其 CSRF token 与上游 cookie 一致）。"""
    try:
        for c in get_session():
            if c.name == "operon_csrf":
                return c.value
    except Exception:
        pass
    return None


def _pipe(src: socket.socket, dst: socket.socket) -> None:
    """单向透明中继：src -> dst，直到任一端关闭。"""
    try:
        while True:
            data = src.recv(4096)
            if not data:
                break
            dst.sendall(data)
    except Exception:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except Exception:
            pass


def _enable_keepalive(sock: socket.socket, idle: int = 30, intvl: int = 10, cnt: int = 3) -> None:
    """开 TCP keepalive：既不误杀空闲长连（与 recv 超时不同，keepalive 不影响数据收发），
    又能在对端真死（断网/NAT 超时）时及时探出，而不是无限挂着。
    跨平台：Linux 用 TCP_KEEPIDLE/INTVL/CNT；Windows 用 SIO_KEEPALIVE_VALS ioctl
    （Windows 没有 TCP_KEEPIDLE 等常量，只有 SO_KEEPALIVE 的话默认 2 小时太长，WS 易掉）。"""
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        if hasattr(socket, "TCP_KEEPIDLE"):  # Linux/macOS
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, idle)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, intvl)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, cnt)
        if os.name == "nt" and hasattr(socket, "SIO_KEEPALIVE_VALS"):  # Windows
            # (onoff, idle_ms, interval_ms)
            sock.ioctl(socket.SIO_KEEPALIVE_VALS, (1, idle * 1000, intvl * 1000))
    except (OSError, AttributeError):
        pass


class ProxyHandler(http.server.BaseHTTPRequestHandler):
    # 由各自 server 实例注入：上游端口 + 是否改写 8990/8991
    # 通过 self.server.upstream_port 访问
    protocol_version = "HTTP/1.1"
    server_version = "LANBridgeGateway/1.0"

    # ---- 鉴权 ----
    def _client_token(self):
        t = None
        parsed = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(parsed.query)
        t = q.get("token", [None])[0]
        if not t:
            t = self.headers.get("Authorization", "")
            if t.lower().startswith("bearer "):
                t = t[7:].strip()
            else:
                t = None
        if not t:
            cks = self.headers.get("Cookie", "")
            for pair in cks.split(";"):
                k, _, v = pair.strip().partition("=")
                if k == "cs_lan":
                    t = v
                    break
        return t

    def _authorized(self):
        if not GUARD_TOKEN:
            return True  # 未配 token，放行（仅 dev/测试用）
        return secrets.compare_digest(self._client_token() or "", GUARD_TOKEN)

    def _strip_token_query(self):
        """从 path 里去掉 token= 参数（避免转发给上游）。"""
        parsed = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(parsed.query)
        q.pop("token", None)
        newq = urllib.parse.urlencode(q, doseq=True)
        return parsed._replace(query=newq).geturl() or "/"

    def _build_upstream_cookie(self):
        """合并 cookie：浏览器的 cookie（含 operon_csrf，与其 CSRF token 配对）优先，
        再补 SESSION_JAR 的会话登录 cookie。Science 既能认会话、CSRF 也能对上。"""
        cookies = {}
        for pair in self.headers.get("Cookie", "").split(";"):
            k, _, v = pair.strip().partition("=")
            if k:
                cookies[k] = v
        try:
            for c in get_session():
                cookies.setdefault(c.name, c.value)
        except Exception:
            pass
        return "; ".join(f"{k}={v}" for k, v in cookies.items())

    # ---- 上游转发 ----
    def _forward(self, retry=True):
        upstream_path = self._strip_token_query()
        url = f"http://127.0.0.1:{self.server.upstream_port}{upstream_path}"
        # 读 body
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length > 0 else None
        # 构造上游请求头
        req = urllib.request.Request(url, data=body, method=self.command)
        upstream_origin = f"http://127.0.0.1:{self.server.upstream_port}"
        for k, v in self.headers.items():
            kl = k.lower()
            # origin/referer 用上游自身的源，绕过 Science 的 CSRF/同源校验（forbidden origin）
            if kl in HOP_HEADERS or kl in ("origin", "referer"):
                continue
            req.add_header(k, v)
        req.add_header("Host", f"127.0.0.1:{self.server.upstream_port}")
        req.add_header("Origin", upstream_origin)
        req.add_header("Referer", upstream_origin + "/")
        # 注入 SESSION_JAR 全量会话 cookie（浏览器自带的 cookie 丢弃，避免旧会话盖过）
        try:
            ck = cookie_header_for(get_session())
            if ck:
                req.add_header("Cookie", ck)
        except Exception as e:
            log(f"代登录态不可用（继续无 cookie 转发）：{e}")
        # 发请求
        try:
            resp = urllib.request.urlopen(req, timeout=CONNECT_TIMEOUT)
        except urllib.error.HTTPError as e:
            resp = e  # 4xx/5xx 也要透传
        except Exception as e:
            self._send_text(502, f"上游连接失败：{e}")
            return
        # 上游 401 -> 刷新会话重试一次
        if getattr(resp, "code", 200) == 401 and retry:
            try:
                refresh_session()
                self._forward(retry=False)
                return
            except Exception as e:
                log(f"刷新会话失败：{e}")
        self._relay(resp)

    def _relay(self, resp):
        status = getattr(resp, "code", 200) or 200
        body = resp.read()
        # 改写响应体里的 localhost:899x -> LAN 端口（压缩体不改写，避免损坏）
        ctype = ""
        encoded = False
        for k, v in resp.headers.items():
            kl = k.lower()
            if kl == "content-type":
                ctype = v.lower()
            elif kl == "content-encoding" and v.strip():
                encoded = True
        host = self.headers.get("Host", "").split(":")[0] or "localhost"
        if not encoded and any(t in ctype for t in ("text/html", "javascript", "text/css", "json", "xml")):
            try:
                body = self._rewrite_body(body, host)
            except Exception:
                pass
        # 响应头：透传非 hop-by-hop，改写 Location/CSP 里的端口
        self.send_response(status)
        sent = set()
        for k, v in resp.headers.items():
            kl = k.lower()
            if kl in HOP_HEADERS or kl in ("content-length",):
                continue
            # 跳过上游的 operon_csrf Set-Cookie，下面用 SESSION_JAR 的值统一覆盖
            if kl == "set-cookie" and v.lower().lstrip().startswith("operon_csrf="):
                continue
            if kl == "location":
                v = self._rewrite_text(v, host)
            if kl == "content-security-policy":
                v = self._rewrite_text(v, host)
            self.send_header(k, v)
            sent.add(kl)
        # 强制浏览器 operon_csrf = SESSION_JAR 的值，使其 CSRF token 与上游收到的 cookie 一致
        csrf = session_operon_csrf()
        if csrf:
            self.send_header("Set-Cookie", f"operon_csrf={csrf}; Path=/; SameSite=Lax")
        # 首次带 ?token= 鉴权通过 -> 下发 cs_lan cookie，浏览器记住（与上游 Set-Cookie 并存）
        if GUARD_TOKEN and "token=" in (self.path or ""):
            self.send_header("Set-Cookie", f"cs_lan={GUARD_TOKEN}; Path=/; SameSite=Lax")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _rewrite_text(self, text: str, host: str) -> str:
        if isinstance(text, bytes):
            text = text.decode("utf-8", "replace")
        text = text.replace(f"localhost:{APP_PORT}", f"{host}:{LAN_PORT}")
        text = text.replace(f"127.0.0.1:{APP_PORT}", f"{host}:{LAN_PORT}")
        text = text.replace(f"localhost:{CONTENT_PORT}", f"{host}:{LAN_CONTENT_PORT}")
        text = text.replace(f"127.0.0.1:{CONTENT_PORT}", f"{host}:{LAN_CONTENT_PORT}")
        return text

    def _rewrite_body(self, body: bytes, host: str) -> bytes:
        return self._rewrite_text(body, host).encode("utf-8")

    def _send_text(self, code, msg):
        b = msg.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(b)

    def _send_json(self, obj):
        import json
        b = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    # ---- WebSocket 升级：Science 前端重度用 WS，必须代理，否则页面空白 ----
    def _handle_ws(self):
        upstream_port = self.server.upstream_port
        usock = socket.create_connection(("127.0.0.1", upstream_port), timeout=CONNECT_TIMEOUT)
        # 构造上游握手：转发头（丢弃浏览器 cookie/origin）+ 注入 SESSION_JAR cookie + 改 Host/Origin
        upstream_path = self._strip_token_query()
        ck = ""
        try:
            ck = cookie_header_for(get_session())
        except Exception:
            pass
        lines = [f"{self.command} {upstream_path} HTTP/1.1"]
        for k, v in self.headers.items():
            kl = k.lower()
            if kl in ("host", "origin", "referer", "cookie"):
                continue
            lines.append(f"{k}: {v}")
        if ck:
            lines.append(f"Cookie: {ck}")
        lines.append(f"Host: 127.0.0.1:{upstream_port}")
        lines.append(f"Origin: http://127.0.0.1:{upstream_port}")
        usock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        # 读上游响应到响应头结束，整段转发给客户端（含 101 和任何早期帧）
        buf = b""
        usock.settimeout(CONNECT_TIMEOUT)
        while b"\r\n\r\n" not in buf:
            chunk = usock.recv(4096)
            if not chunk:
                break
            buf += chunk
        self.wfile.write(buf)
        self.wfile.flush()
        # 握手已结束；中继期间必须清掉上面设的 10s 超时，否则上游静默 >10s
        # recv 就会抛 timeout，_pipe 的 finally 会拆连接 -> 浏览器 WS 掉线重连。
        usock.settimeout(None)
        _enable_keepalive(usock)
        _enable_keepalive(self.connection)
        # 双向透明中继，直到任一端关闭
        t = threading.Thread(target=_pipe, args=(self.connection, usock), daemon=True)
        t.start()
        _pipe(usock, self.connection)
        t.join(timeout=1)
        try:
            usock.close()
        except Exception:
            pass

    # ---- HTTP 方法 ----
    def _handle(self):
        path_only = urllib.parse.urlparse(self.path).path
        if path_only == "/__lan_health":
            if not self._authorized():
                self._send_text(401, "unauthorized")
                return
            ok = True
            try:
                jar = get_session()
                ok = len(jar) > 0
            except Exception as e:
                ok = False
            self._send_json({"running": True, "session_acquired": ok,
                             "app_port": APP_PORT, "content_port": CONTENT_PORT,
                             "lan_port": LAN_PORT, "lan_content_port": LAN_CONTENT_PORT})
            return
        if not self._authorized():
            self._send_text(401, "unauthorized")
            return
        if self.headers.get("Upgrade", "").lower() == "websocket":
            try:
                self._handle_ws()
            except Exception as e:
                try:
                    self._send_text(502, f"WS 网关异常：{e}")
                except Exception:
                    pass
            return
        try:
            self._forward()
        except Exception as e:
            try:
                self._send_text(502, f"网关转发异常：{e}")
            except Exception:
                pass

    def do_GET(self): self._handle()
    def do_POST(self): self._handle()
    def do_PUT(self): self._handle()
    def do_PATCH(self): self._handle()
    def do_DELETE(self): self._handle()
    def do_HEAD(self): self._handle()
    def do_OPTIONS(self): self._handle()

    def log_message(self, fmt, *args):
        log(f"{self.command} {self.path} -> {args[1] if len(args) > 1 else ''}")


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class SSLThreadingHTTPServer(ThreadingHTTPServer):
    """HTTPS：accept 后在限时内做 TLS 握手。坏客户端（给 HTTPS 端口发明文 HTTP、
    或不完成握手）快速失败或 15s 超时——既不永久阻塞 accept 主线程，也不会把进程搞崩。"""
    def __init__(self, addr, handler, ssl_ctx):
        super().__init__(addr, handler)
        self.ssl_ctx = ssl_ctx

    def get_request(self):
        conn, addr = self.socket.accept()
        conn.settimeout(15.0)
        try:
            conn = self.ssl_ctx.wrap_socket(conn, server_side=True)
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            raise OSError("TLS handshake failed")
        conn.settimeout(None)  # 握手成功，交给 handler（含长连 WS）自己管
        return conn, addr

    def handle_error(self, request, client_address):
        etype, evalue, _ = sys.exc_info()
        name = etype.__name__ if etype else "Error"
        msg = str(evalue)[:100] if evalue else ""
        if isinstance(etype, (ssl.SSLError, OSError, ConnectionError, BrokenPipeError)):
            log(f"连接异常（已忽略，不影响网关）: {name}: {msg}")
        else:
            log(f"处理异常: {name}: {msg}")


def main():
    _redirect_stdio_to_log()
    log(f"===== lan-gateway (Windows) 启动（pid={os.getpid()}，日志：{LOG_PATH}）=====")
    for name, val in [("LAN_PORT", LAN_PORT), ("LAN_CONTENT_PORT", LAN_CONTENT_PORT),
                      ("APP_PORT", APP_PORT), ("CONTENT_PORT", CONTENT_PORT),
                      ("WSL_DISTRO", WSL_DISTRO or "(default)")]:
        log(f"配置 {name}={val}")
    if not GUARD_TOKEN:
        log("⚠️ CS_GUARD_TOKEN 未设置，网关无鉴权（仅适合本机调试）")
    # 启动时先尝试代登录一次（失败不退出，后续请求/401 会重试）
    try:
        get_session()
    except Exception as e:
        log(f"启动代登录失败（WSL/Science 可能还没起，稍后请求触发重试）：{e}")
    ensure_cert()
    ssl_ctx = make_ssl_context()
    srv_app = SSLThreadingHTTPServer(("0.0.0.0", LAN_PORT), ProxyHandler, ssl_ctx)
    srv_app.upstream_port = APP_PORT
    srv_content = SSLThreadingHTTPServer(("0.0.0.0", LAN_CONTENT_PORT), ProxyHandler, ssl_ctx)
    srv_content.upstream_port = CONTENT_PORT
    threading.Thread(target=srv_content.serve_forever, daemon=True).start()
    log(f"内容端口 {LAN_CONTENT_PORT}(https) -> {CONTENT_PORT} 已监听")
    log(f"应用端口 {LAN_PORT}(https) -> {APP_PORT} 已监听，开始服务（首次打开需信任自签证书）")
    try:
        srv_app.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
