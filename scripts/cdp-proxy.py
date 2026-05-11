#!/usr/bin/env python3
"""CDP Proxy - 通过 HTTP API 操控用户日常 Chrome（纯 Python，无 Node 依赖）
要求：Chrome 已开启 --remote-debugging-port
Python 3.8+，依赖：websocket-client（pip install websocket-client）
"""

import base64
import json
import os
import platform
import socket
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

try:
    import websocket  # websocket-client
except ImportError:
    print("[CDP Proxy] 错误：缺少依赖 websocket-client")
    print("  解决方案：pip install websocket-client  或  uv pip install websocket-client")
    sys.exit(1)

PORT = int(os.environ.get("CDP_PROXY_PORT", "3456"))

# --- 全局状态 ---
ws_app: "websocket.WebSocketApp | None" = None
ws_lock = threading.Lock()
cmd_id = 0
cmd_id_lock = threading.Lock()
pending: dict = {}  # id -> threading.Event + result
pending_lock = threading.Lock()
sessions: dict = {}  # targetId -> sessionId
sessions_lock = threading.Lock()
managed_tabs: dict = {}  # targetId -> {"last_accessed": float}
managed_tabs_lock = threading.Lock()
TAB_IDLE_TIMEOUT = float(os.environ.get("CDP_TAB_IDLE_TIMEOUT", "900000")) / 1000.0  # 15 min default, convert ms to seconds
CLEANUP_INTERVAL = 60.0  # sweep every 60s
port_guarded_sessions: set = set()
chrome_port: int | None = None
chrome_ws_path: str | None = None
connect_lock = threading.Lock()
ws_connected = threading.Event()
ws_error: str | None = None


# --- Chrome 调试端口发现 ---

def check_port(port: int, host: str = "127.0.0.1", timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def active_port_files() -> list:
    home = Path.home()
    system = platform.system()
    if system == "Darwin":
        return [
            home / "Library/Application Support/Google/Chrome/DevToolsActivePort",
            home / "Library/Application Support/Google/Chrome Canary/DevToolsActivePort",
            home / "Library/Application Support/Chromium/DevToolsActivePort",
        ]
    elif system == "Linux":
        return [
            home / ".config/google-chrome/DevToolsActivePort",
            home / ".config/chromium/DevToolsActivePort",
        ]
    elif system == "Windows":
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        return [
            Path(local_app_data) / "Google/Chrome/User Data/DevToolsActivePort",
            Path(local_app_data) / "Chromium/User Data/DevToolsActivePort",
        ]
    return []


def discover_chrome_port() -> tuple[int, str | None] | None:
    for path in active_port_files():
        try:
            lines = path.read_text(encoding="utf-8").strip().splitlines()
            port = int(lines[0])
            if 0 < port < 65536 and check_port(port):
                ws_path = lines[1].strip() if len(lines) > 1 else None
                print(f"[CDP Proxy] 从 DevToolsActivePort 发现端口: {port}"
                      f"{' (带 wsPath)' if ws_path else ''}")
                return port, ws_path
        except Exception:
            pass
    for port in [9222, 9229, 9333]:
        if check_port(port):
            print(f"[CDP Proxy] 扫描发现 Chrome 调试端口: {port}")
            return port, None
    return None


def get_ws_url(port: int, ws_path: str | None) -> str:
    if ws_path:
        return f"ws://127.0.0.1:{port}{ws_path}"
    return f"ws://127.0.0.1:{port}/devtools/browser"


# --- WebSocket 消息处理 ---

def on_message(ws, message):
    global pending, sessions
    try:
        msg = json.loads(message)
    except Exception:
        return

    method = msg.get("method", "")

    if method == "Target.attachedToTarget":
        params = msg.get("params", {})
        sid = params.get("sessionId")
        target_id = params.get("targetInfo", {}).get("targetId")
        if sid and target_id:
            with sessions_lock:
                sessions[target_id] = sid

    if method == "Fetch.requestPaused":
        params = msg.get("params", {})
        request_id = params.get("requestId")
        session_id = params.get("sessionId")
        if request_id:
            threading.Thread(
                target=lambda: send_cdp_sync(
                    "Fetch.failRequest",
                    {"requestId": request_id, "errorReason": "ConnectionRefused"},
                    session_id
                ),
                daemon=True,
            ).start()

    msg_id = msg.get("id")
    if msg_id is not None:
        with pending_lock:
            entry = pending.get(msg_id)
        if entry:
            entry["result"] = msg
            entry["event"].set()


def on_open(ws):
    global ws_error
    ws_error = None
    ws_connected.set()
    print(f"[CDP Proxy] 已连接 Chrome (端口 {chrome_port})")


def on_error(ws, error):
    global ws_error, chrome_port, chrome_ws_path
    ws_error = str(error)
    chrome_port = None
    chrome_ws_path = None
    ws_connected.set()  # unblock waiters
    print(f"[CDP Proxy] 连接错误: {error}（端口缓存已清除，下次将重新发现）")


def on_close(ws, close_status_code, close_msg):
    global chrome_port, chrome_ws_path
    chrome_port = None
    chrome_ws_path = None
    with sessions_lock:
        sessions.clear()
    with managed_tabs_lock:
        managed_tabs.clear()
    ws_connected.clear()
    print("[CDP Proxy] 连接断开")


# --- 连接管理 ---

def connect():
    global ws_app, chrome_port, chrome_ws_path, ws_error

    with connect_lock:
        if ws_app and ws_app.sock and ws_app.sock.connected:
            return

        if not chrome_port:
            discovered = discover_chrome_port()
            if not discovered:
                raise RuntimeError(
                    "Chrome 未开启远程调试端口。请用以下方式启动 Chrome：\n"
                    "  macOS: /Applications/Google\\ Chrome.app/Contents/MacOS/Google\\ Chrome --remote-debugging-port=9222\n"
                    "  Linux: google-chrome --remote-debugging-port=9222\n"
                    "  或在 chrome://flags 中搜索 'remote debugging' 并启用"
                )
            chrome_port, chrome_ws_path = discovered

        url = get_ws_url(chrome_port, chrome_ws_path)
        ws_connected.clear()
        ws_error = None

        ws_app = websocket.WebSocketApp(
            url,
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
        )
        t = threading.Thread(target=lambda: ws_app.run_forever(ping_interval=20), daemon=True)
        t.start()

        ws_connected.wait(timeout=8)
        if ws_error:
            raise RuntimeError(ws_error)
        if not (ws_app.sock and ws_app.sock.connected):
            raise RuntimeError("WebSocket 连接超时")


def send_cdp(method: str, params: dict = None, session_id: str = None, timeout: float = 30.0) -> dict:
    global cmd_id
    if params is None:
        params = {}

    if not (ws_app and ws_app.sock and ws_app.sock.connected):
        raise RuntimeError("WebSocket 未连接")

    with cmd_id_lock:
        cmd_id += 1
        _id = cmd_id

    msg = {"id": _id, "method": method, "params": params}
    if session_id:
        msg["sessionId"] = session_id

    event = threading.Event()
    entry = {"event": event, "result": None}
    with pending_lock:
        pending[_id] = entry

    ws_app.send(json.dumps(msg))

    if not event.wait(timeout=timeout):
        with pending_lock:
            pending.pop(_id, None)
        raise RuntimeError(f"CDP 命令超时: {method}")

    with pending_lock:
        pending.pop(_id, None)
    return entry["result"]


def send_cdp_sync(method: str, params: dict = None, session_id: str = None):
    """非阻塞辅助调用（用于事件回调中）"""
    try:
        send_cdp(method, params or {}, session_id, timeout=5.0)
    except Exception:
        pass


# --- Port guard ---

def enable_port_guard(session_id: str):
    if not chrome_port or session_id in port_guarded_sessions:
        return
    try:
        send_cdp("Fetch.enable", {
            "patterns": [
                {"urlPattern": f"http://127.0.0.1:{chrome_port}/*", "requestStage": "Request"},
                {"urlPattern": f"http://localhost:{chrome_port}/*", "requestStage": "Request"},
            ]
        }, session_id)
        port_guarded_sessions.add(session_id)
    except Exception:
        pass


# --- 闲置 Tab 自动清理 ---

def touch_tab(target_id: str):
    with managed_tabs_lock:
        entry = managed_tabs.get(target_id)
        if entry:
            entry["last_accessed"] = time.monotonic()


def cleanup_idle_tabs():
    now = time.monotonic()
    to_close = []
    with managed_tabs_lock:
        for target_id, info in list(managed_tabs.items()):
            if now - info["last_accessed"] >= TAB_IDLE_TIMEOUT:
                to_close.append(target_id)
    for target_id in to_close:
        try:
            send_cdp("Target.closeTarget", {"targetId": target_id}, timeout=5.0)
        except Exception:
            pass
        with sessions_lock:
            sessions.pop(target_id, None)
        with managed_tabs_lock:
            managed_tabs.pop(target_id, None)
        print(f"[CDP Proxy] Auto-closed idle tab: {target_id}")


def close_all_managed_tabs():
    targets = []
    with managed_tabs_lock:
        targets = list(managed_tabs.keys())
    for target_id in targets:
        try:
            send_cdp("Target.closeTarget", {"targetId": target_id}, timeout=5.0)
        except Exception:
            pass
        with sessions_lock:
            sessions.pop(target_id, None)
        with managed_tabs_lock:
            managed_tabs.pop(target_id, None)
    if targets:
        print(f"[CDP Proxy] Shutdown: closed {len(targets)} managed tab(s)")


def ensure_session(target_id: str) -> str:
    with sessions_lock:
        if target_id in sessions:
            return sessions[target_id]

    resp = send_cdp("Target.attachToTarget", {"targetId": target_id, "flatten": True})
    sid = resp.get("result", {}).get("sessionId")
    if not sid:
        raise RuntimeError(f"attach 失败: {resp.get('error')}")
    with sessions_lock:
        sessions[target_id] = sid
    threading.Thread(target=lambda: enable_port_guard(sid), daemon=True).start()
    return sid


def wait_for_load(session_id: str, timeout: float = 15.0):
    send_cdp("Page.enable", {}, session_id)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            resp = send_cdp("Runtime.evaluate", {
                "expression": "document.readyState",
                "returnByValue": True,
            }, session_id)
            if resp.get("result", {}).get("result", {}).get("value") == "complete":
                return
        except Exception:
            pass
        time.sleep(0.5)


# --- HTTP Server ---

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # 静默访问日志

    def send_json(self, data, status: int = 200):
        body = json.dumps(data, ensure_ascii=False, indent=2).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_body(self) -> str:
        length = int(self.headers.get("Content-Length", 0))
        if length:
            return self.rfile.read(length).decode("utf-8")
        return ""

    def handle_request(self):
        parsed = urlparse(self.path)
        pathname = parsed.path
        q = {k: v[0] for k, v in parse_qs(parsed.query).items()}

        try:
            if pathname == "/health":
                connected = bool(ws_app and ws_app.sock and ws_app.sock.connected)
                self.send_json({"status": "ok", "connected": connected,
                                "sessions": len(sessions),
                                "managedTabs": len(managed_tabs),
                                "chromePort": chrome_port})
                return

            connect()

            if q.get("target"):
                touch_tab(q["target"])

            if pathname == "/targets":
                resp = send_cdp("Target.getTargets")
                pages = [t for t in resp.get("result", {}).get("targetInfos", [])
                         if t.get("type") == "page"]
                self.send_json(pages)

            elif pathname == "/new":
                target_url = q.get("url", "about:blank")
                resp = send_cdp("Target.createTarget", {"url": target_url, "background": True})
                target_id = resp["result"]["targetId"]
                with managed_tabs_lock:
                    managed_tabs[target_id] = {"last_accessed": time.monotonic()}
                if target_url != "about:blank":
                    try:
                        sid = ensure_session(target_id)
                        wait_for_load(sid)
                    except Exception:
                        pass
                self.send_json({"targetId": target_id})

            elif pathname == "/close":
                resp = send_cdp("Target.closeTarget", {"targetId": q.get("target")})
                with sessions_lock:
                    sessions.pop(q.get("target"), None)
                with managed_tabs_lock:
                    managed_tabs.pop(q.get("target"), None)
                self.send_json(resp.get("result", {}))

            elif pathname == "/navigate":
                sid = ensure_session(q["target"])
                resp = send_cdp("Page.navigate", {"url": q["url"]}, sid)
                wait_for_load(sid)
                self.send_json(resp.get("result", {}))

            elif pathname == "/back":
                sid = ensure_session(q["target"])
                send_cdp("Runtime.evaluate", {"expression": "history.back()"}, sid)
                wait_for_load(sid)
                self.send_json({"ok": True})

            elif pathname == "/eval":
                sid = ensure_session(q["target"])
                expr = self.read_body() or q.get("expr", "document.title")
                resp = send_cdp("Runtime.evaluate", {
                    "expression": expr,
                    "returnByValue": True,
                    "awaitPromise": True,
                }, sid)
                result = resp.get("result", {})
                val = result.get("result", {}).get("value")
                if val is not None:
                    self.send_json({"value": val})
                elif result.get("exceptionDetails"):
                    self.send_json({"error": result["exceptionDetails"].get("text")}, 400)
                else:
                    self.send_json(result)

            elif pathname == "/click":
                sid = ensure_session(q["target"])
                selector = self.read_body()
                if not selector:
                    self.send_json({"error": "POST body 需要 CSS 选择器"}, 400)
                    return
                sel_json = json.dumps(selector)
                js = f"""(() => {{
                    const el = document.querySelector({sel_json});
                    if (!el) return {{ error: '未找到元素: ' + {sel_json} }};
                    el.scrollIntoView({{ block: 'center' }});
                    el.click();
                    return {{ clicked: true, tag: el.tagName, text: (el.textContent || '').slice(0, 100) }};
                }})()"""
                resp = send_cdp("Runtime.evaluate", {
                    "expression": js, "returnByValue": True, "awaitPromise": True
                }, sid)
                val = resp.get("result", {}).get("result", {}).get("value")
                if val:
                    status = 400 if val.get("error") else 200
                    self.send_json(val, status)
                else:
                    self.send_json(resp.get("result", {}))

            elif pathname == "/clickAt":
                sid = ensure_session(q["target"])
                selector = self.read_body()
                if not selector:
                    self.send_json({"error": "POST body 需要 CSS 选择器"}, 400)
                    return
                sel_json = json.dumps(selector)
                js = f"""(() => {{
                    const el = document.querySelector({sel_json});
                    if (!el) return {{ error: '未找到元素: ' + {sel_json} }};
                    el.scrollIntoView({{ block: 'center' }});
                    const rect = el.getBoundingClientRect();
                    return {{ x: rect.x + rect.width / 2, y: rect.y + rect.height / 2,
                              tag: el.tagName, text: (el.textContent || '').slice(0, 100) }};
                }})()"""
                coord_resp = send_cdp("Runtime.evaluate", {
                    "expression": js, "returnByValue": True, "awaitPromise": True
                }, sid)
                coord = coord_resp.get("result", {}).get("result", {}).get("value")
                if not coord or coord.get("error"):
                    self.send_json(coord or coord_resp.get("result", {}), 400)
                    return
                for event_type in ("mousePressed", "mouseReleased"):
                    send_cdp("Input.dispatchMouseEvent", {
                        "type": event_type, "x": coord["x"], "y": coord["y"],
                        "button": "left", "clickCount": 1
                    }, sid)
                self.send_json({"clicked": True, "x": coord["x"], "y": coord["y"],
                                "tag": coord["tag"], "text": coord["text"]})

            elif pathname == "/setFiles":
                sid = ensure_session(q["target"])
                body = json.loads(self.read_body())
                if not body.get("selector") or not body.get("files"):
                    self.send_json({"error": "需要 selector 和 files 字段"}, 400)
                    return
                send_cdp("DOM.enable", {}, sid)
                doc = send_cdp("DOM.getDocument", {}, sid)
                node = send_cdp("DOM.querySelector", {
                    "nodeId": doc["result"]["root"]["nodeId"],
                    "selector": body["selector"],
                }, sid)
                if not node.get("result", {}).get("nodeId"):
                    self.send_json({"error": f"未找到元素: {body['selector']}"}, 400)
                    return
                send_cdp("DOM.setFileInputFiles", {
                    "nodeId": node["result"]["nodeId"],
                    "files": body["files"],
                }, sid)
                self.send_json({"success": True, "files": len(body["files"])})

            elif pathname == "/scroll":
                sid = ensure_session(q["target"])
                y = int(q.get("y", "3000"))
                direction = q.get("direction", "down")
                if direction == "top":
                    js = 'window.scrollTo(0, 0); "scrolled to top"'
                elif direction == "bottom":
                    js = 'window.scrollTo(0, document.body.scrollHeight); "scrolled to bottom"'
                elif direction == "up":
                    js = f'window.scrollBy(0, -{abs(y)}); "scrolled up {abs(y)}px"'
                else:
                    js = f'window.scrollBy(0, {abs(y)}); "scrolled down {abs(y)}px"'
                resp = send_cdp("Runtime.evaluate", {
                    "expression": js, "returnByValue": True
                }, sid)
                time.sleep(0.8)
                self.send_json({"value": resp.get("result", {}).get("result", {}).get("value")})

            elif pathname == "/screenshot":
                sid = ensure_session(q["target"])
                fmt = q.get("format", "png")
                resp = send_cdp("Page.captureScreenshot", {
                    "format": fmt,
                    **({"quality": 80} if fmt == "jpeg" else {}),
                }, sid)
                data = base64.b64decode(resp["result"]["data"])
                file_path = q.get("file")
                if file_path:
                    Path(file_path).write_bytes(data)
                    self.send_json({"saved": file_path})
                else:
                    self.send_response(200)
                    self.send_header("Content-Type", f"image/{fmt}")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return

            elif pathname == "/printPDF":
                sid = ensure_session(q["target"])
                landscape = q.get("landscape", "").lower() == "true"
                paper_width = float(q.get("paperWidth", "210")) / 25.4
                paper_height = float(q.get("paperHeight", "297")) / 25.4
                resp = send_cdp("Page.printToPDF", {
                    "landscape": landscape,
                    "paperWidth": paper_width,
                    "paperHeight": paper_height,
                    "marginTop": float(q.get("marginTop", "31.8")) / 25.4,
                    "marginBottom": float(q.get("marginBottom", "31.8")) / 25.4,
                    "marginLeft": float(q.get("marginLeft", "25.4")) / 25.4,
                    "marginRight": float(q.get("marginRight", "25.4")) / 25.4,
                    "printBackground": q.get("printBackground", "true") != "false",
                    "preferCSSPageSize": q.get("preferCSSPageSize", "").lower() == "true",
                }, sid)
                data = base64.b64decode(resp["result"]["data"])
                file_path = q.get("file")
                if file_path:
                    Path(file_path).write_bytes(data)
                    self.send_json({"saved": file_path})
                else:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/pdf")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return

            elif pathname == "/info":
                sid = ensure_session(q["target"])
                resp = send_cdp("Runtime.evaluate", {
                    "expression": "JSON.stringify({title: document.title, url: location.href, ready: document.readyState})",
                    "returnByValue": True,
                }, sid)
                raw = resp.get("result", {}).get("result", {}).get("value", "{}")
                self.send_json(json.loads(raw))

            else:
                self.send_json({
                    "error": "未知端点",
                    "endpoints": {
                        "/health": "GET - 健康检查",
                        "/targets": "GET - 列出所有页面 tab",
                        "/new?url=": "GET - 创建新后台 tab（自动等待加载）",
                        "/close?target=": "GET - 关闭 tab",
                        "/navigate?target=&url=": "GET - 导航（自动等待加载）",
                        "/back?target=": "GET - 后退",
                        "/info?target=": "GET - 页面标题/URL/状态",
                        "/eval?target=": "POST body=JS表达式 - 执行 JS",
                        "/click?target=": "POST body=CSS选择器 - 点击元素",
                        "/clickAt?target=": "POST body=CSS选择器 - 真实鼠标点击",
                        "/setFiles?target=": "POST body=JSON{selector,files} - 文件上传",
                        "/scroll?target=&y=&direction=": "GET - 滚动页面",
                        "/screenshot?target=&file=": "GET - 截图",
                        "/printPDF?target=&file=": "GET - 将页面打印为 PDF（可选参数 format/landscape/margin*）",
                    }
                }, 404)

        except Exception as e:
            self.send_json({"error": str(e)}, 500)

    def do_GET(self):
        self.handle_request()

    def do_POST(self):
        self.handle_request()


def check_port_available(port: int) -> bool:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", port))
        s.close()
        return True
    except OSError:
        return False


def main():
    global PORT

    available = check_port_available(PORT)
    if not available:
        try:
            req = urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=2)
            data = req.read().decode()
            if '"ok"' in data:
                print(f"[CDP Proxy] 已有实例运行在端口 {PORT}，退出")
                sys.exit(0)
        except Exception:
            pass
        print(f"[CDP Proxy] 端口 {PORT} 已被占用")
        sys.exit(1)

    server = HTTPServer(("127.0.0.1", PORT), Handler)
    print(f"[CDP Proxy] 运行在 http://localhost:{PORT}")

    # 启动时尝试连接 Chrome（非阻塞）
    def try_connect():
        try:
            connect()
        except Exception as e:
            print(f"[CDP Proxy] 初始连接失败: {e}（将在首次请求时重试）")
    threading.Thread(target=try_connect, daemon=True).start()

    def cleanup_loop():
        while True:
            time.sleep(CLEANUP_INTERVAL)
            cleanup_idle_tabs()
    timer_thread = threading.Thread(target=cleanup_loop, daemon=True)
    timer_thread.start()

    def shutdown(sig: str):
        print(f"[CDP Proxy] {sig}, cleaning up...")
        close_all_managed_tabs()
        os._exit(0)

    import signal
    signal.signal(signal.SIGINT, lambda s, f: shutdown("SIGINT"))
    signal.signal(signal.SIGTERM, lambda s, f: shutdown("SIGTERM"))

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        shutdown("SIGINT")


if __name__ == "__main__":
    main()
