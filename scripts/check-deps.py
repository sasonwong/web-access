#!/usr/bin/env python3
"""环境检查 + 确保 CDP Proxy 就绪（跨平台，纯 Python，无 Node 依赖）"""

import json
import os
import platform
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROXY_SCRIPT = ROOT / "scripts" / "cdp-proxy.py"
PROXY_PORT = int(os.environ.get("CDP_PROXY_PORT", "3456"))


# --- Python 版本检查 ---

def check_python():
    major, minor = sys.version_info[:2]
    version = f"{major}.{minor}"
    if (major, minor) >= (3, 8):
        print(f"python: ok ({version})")
    else:
        print(f"python: warn ({version}, 建议升级到 3.8+)")


# --- TCP 端口探测 ---

def check_port(port: int, host: str = "127.0.0.1", timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# --- Chrome 调试端口检测 ---

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


def detect_chrome_port() -> int | None:
    for path in active_port_files():
        try:
            lines = path.read_text(encoding="utf-8").strip().splitlines()
            port = int(lines[0])
            if 0 < port < 65536 and check_port(port):
                return port
        except Exception:
            pass
    for port in [9222, 9229, 9333]:
        if check_port(port):
            return port
    return None


# --- CDP Proxy 启动与等待 ---

def http_get_json(url: str, timeout: float = 3.0):
    try:
        req = urllib.request.urlopen(url, timeout=timeout)
        return json.loads(req.read().decode())
    except Exception:
        return None


def start_proxy_detached():
    import tempfile
    log_path = Path(tempfile.gettempdir()) / "cdp-proxy.log"
    with open(log_path, "a") as log_fd:
        kwargs = dict(
            stdout=log_fd,
            stderr=log_fd,
            stdin=subprocess.DEVNULL,
        )
        if platform.system() == "Windows":
            kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen([sys.executable, str(PROXY_SCRIPT)], **kwargs)


def ensure_proxy() -> bool:
    targets_url = f"http://127.0.0.1:{PROXY_PORT}/targets"

    targets = http_get_json(targets_url)
    if isinstance(targets, list):
        print("proxy: ready")
        return True

    print("proxy: connecting...")
    start_proxy_detached()
    time.sleep(2)

    for i in range(1, 16):
        result = http_get_json(targets_url, timeout=8.0)
        if isinstance(result, list):
            print("proxy: ready")
            return True
        if i == 1:
            print("⚠️  Chrome 可能有授权弹窗，请点击「允许」后等待连接...")
        time.sleep(1)

    import tempfile
    log_path = Path(tempfile.gettempdir()) / "cdp-proxy.log"
    print("❌ 连接超时，请检查 Chrome 调试设置")
    print(f"  日志：{log_path}")
    return False


# --- main ---

def main():
    check_python()

    chrome_port = detect_chrome_port()
    if not chrome_port:
        print(
            "chrome: not connected — 请确保 Chrome 已打开，"
            "然后访问 chrome://inspect/#remote-debugging 并勾选 Allow remote debugging"
        )
        sys.exit(1)
    print(f"chrome: ok (port {chrome_port})")

    proxy_ok = ensure_proxy()
    if not proxy_ok:
        sys.exit(1)

    patterns_dir = ROOT / "references" / "site-patterns"
    try:
        sites = [f.stem for f in patterns_dir.iterdir() if f.suffix == ".md"]
        if sites:
            print(f"\nsite-patterns: {', '.join(sites)}")
    except Exception:
        pass


if __name__ == "__main__":
    main()
