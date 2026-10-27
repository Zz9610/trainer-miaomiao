#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
骑行台控制 —— 本地应用服务

作用：
  1. 提供网页（index.html）的静态访问
  2. 把个人资料 / 骑行记录 / 训练计划 / 设备记忆 / 断点快照
     保存成 data/ 目录下的普通 JSON 文件，可直接查看、备份、删除

启动：双击 start.bat，或 python server.py
"""

import http.server
import json
import os
import queue
import socket
import sys
import threading
import time
import urllib.parse
import webbrowser

ROOT = os.path.dirname(os.path.abspath(__file__))

# 打包成 exe 后：
#   APP_DIR = exe 所在目录（数据目录放这里，跟着 exe 走）
#   RES_DIR = PyInstaller 解压出的资源目录（html / vendor）
if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(sys.executable)
    RES_DIR = getattr(sys, "_MEIPASS", APP_DIR)
else:
    APP_DIR = ROOT
    RES_DIR = ROOT

DATA_DIR = os.path.join(APP_DIR, "data")
VENDOR_DIR = os.path.join(RES_DIR, "vendor")
KEYS = ("profile", "rides", "plan", "devices", "snapshot", "ftp")
BASE_PORT = 8000
MAX_PORT_TRIES = 20

# 让 vendor/ 里的 bleak 可用（不污染系统 Python）
if os.path.isdir(VENDOR_DIR):
    sys.path.insert(0, VENDOR_DIR)

try:
    from ble_gateway import gateway
    BLE_READY = True
    BLE_IMPORT_ERR = ""
except Exception as _e:      # 没有 bleak 也能正常当静态服务器用
    gateway = None
    BLE_READY = False
    BLE_IMPORT_ERR = f"{type(_e).__name__}: {_e}"


def ensure_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


def file_of(key):
    return os.path.join(DATA_DIR, key + ".json")


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=RES_DIR, **kwargs)

    # 控制台安静一点
    def log_message(self, fmt, *args):
        pass

    def _send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path

        # ---- 蓝牙：SSE 事件流 ----
        if path == "/api/ble/events":
            return self._sse()

        if path == "/api/ble/status":
            if not BLE_READY:
                return self._send_json({"ok": False, "available": False, "error": BLE_IMPORT_ERR})
            st = gateway.status()
            st.update({"ok": True, "available": True})
            return self._send_json(st)

        if path == "/api/ble/services":
            if not BLE_READY:
                return self._send_json({"ok": False, "error": BLE_IMPORT_ERR})
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            addr = (qs.get("address", [""])[0] or None)
            return self._send_json({"ok": True, "services": gateway.services(addr)})

        if path == "/api/ble/scan":
            if not BLE_READY:
                return self._send_json({"ok": False, "error": BLE_IMPORT_ERR})
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                t = float(qs.get("timeout", ["6"])[0])
            except Exception:
                t = 6.0
            return self._send_json({"ok": True, "devices": gateway.scan(t)})

        # ---- 数据 ----
        if path == "/api/load":
            out = {}
            for k in KEYS:
                p = file_of(k)
                if os.path.exists(p):
                    try:
                        with open(p, encoding="utf-8") as f:
                            out[k] = json.load(f)
                    except Exception as e:
                        print(f"[warn] 读取 {p} 失败: {e}")
            return self._send_json({"ok": True, "data": out, "dir": DATA_DIR})

        if path == "/api/ping":
            return self._send_json({"ok": True, "dir": DATA_DIR, "ble": BLE_READY,
                                    "ble_error": BLE_IMPORT_ERR})

        return super().do_GET()

    def _sse(self):
        if not BLE_READY:
            return self._send_json({"ok": False, "error": BLE_IMPORT_ERR}, 503)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        q = gateway.add_sub()
        try:
            self.wfile.write(b": hello\n\n")
            self.wfile.flush()
            while True:
                try:
                    ev = q.get(timeout=15)
                    payload = json.dumps(ev, ensure_ascii=False)
                    self.wfile.write(("data: " + payload + "\n\n").encode("utf-8"))
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except Exception:
            pass
        finally:
            gateway.remove_sub(q)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"

        # ---- 蓝牙 ----
        if path.startswith("/api/ble/") and not BLE_READY:
            return self._send_json({"ok": False, "error": BLE_IMPORT_ERR}, 503)

        if path == "/api/ble/connect":
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except Exception:
                body = {}
            addr = (body.get("address") or "").strip()
            if not addr:
                return self._send_json({"ok": False, "error": "缺少 address"}, 400)
            return self._send_json({"ok": True, "result": gateway.connect(addr)})

        if path == "/api/ble/disconnect":
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except Exception:
                body = {}
            return self._send_json({"ok": True, "result": gateway.disconnect(body.get("address") or None)})

        if path == "/api/ble/subscribe":
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except Exception:
                body = {}
            uuids = body.get("uuids") or []
            return self._send_json({"ok": True,
                                    "result": gateway.subscribe(body.get("address") or "", uuids)})

        if path == "/api/ble/write":
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except Exception:
                body = {}
            return self._send_json(gateway.write(
                body.get("address") or "", body.get("uuid", ""),
                body.get("hex", ""), body.get("response", True)))

        if path == "/api/ble/read":
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except Exception:
                body = {}
            return self._send_json(gateway.read(body.get("address") or "", body.get("uuid", "")))

        # ---- 数据 ----
        if path == "/api/save":
            try:
                payload = json.loads(raw.decode("utf-8"))
            except Exception as e:
                return self._send_json({"ok": False, "err": f"bad json: {e}"}, 400)
            saved = []
            for k, v in payload.items():
                if k not in KEYS:
                    continue
                p = file_of(k)
                tmp = p + ".tmp"
                try:
                    with open(tmp, "w", encoding="utf-8") as f:
                        json.dump(v, f, ensure_ascii=False, indent=2)
                    os.replace(tmp, p)
                    saved.append(k)
                except Exception as e:
                    print(f"[error] 写入 {p} 失败: {e}")
                    return self._send_json({"ok": False, "err": str(e)}, 500)
            return self._send_json({"ok": True, "saved": saved})

        if path == "/api/open":
            try:
                os.startfile(DATA_DIR)  # 仅 Windows
                return self._send_json({"ok": True, "dir": DATA_DIR})
            except Exception as e:
                return self._send_json({"ok": False, "err": str(e)}, 500)

        return self._send_json({"ok": False, "err": "unknown api"}, 404)


def pick_port():
    for i in range(MAX_PORT_TRIES):
        port = BASE_PORT + i
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return BASE_PORT


def main():
    ensure_dir()
    port = pick_port()
    url = f"http://localhost:{port}/index.html"

    print("=" * 56, flush=True)
    print("  骑行台控制", flush=True)
    print(f"  页面: {url}", flush=True)
    print(f"  数据目录: {DATA_DIR}", flush=True)
    print(f"  蓝牙后端: {'可用' if BLE_READY else '不可用 ' + BLE_IMPORT_ERR}", flush=True)
    print("  关闭此窗口即停止服务", flush=True)
    print("=" * 56, flush=True)

    # 延迟打开浏览器，确保服务已就绪（设 TRAINER_NO_BROWSER=1 可跳过，便于自动化测试）
    if not os.environ.get("TRAINER_NO_BROWSER"):
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止", flush=True)
    finally:
        httpd.server_close()


if __name__ == "__main__":
    import traceback
    try:
        main()
    except Exception:
        tb = traceback.format_exc()
        try:
            with open(os.path.join(APP_DIR, "crash.log"), "a", encoding="utf-8") as f:
                f.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n{tb}\n")
        except Exception:
            pass
        print(tb, flush=True)
        try:
            input("\n启动失败，详情已写入 crash.log，按回车退出…")
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()
