# -*- coding: utf-8 -*-
"""
蓝牙网关 —— 用 Windows 原生 BLE（bleak / WinRT）替代浏览器的 Web Bluetooth。

支持同时连接多个设备（骑行台 + 心率计 + 踏频计）：
    clients: {address: BleakClient}
    notify 事件带 address，前端按 address/uuid 区分来源
"""

import asyncio
import queue
import threading

CH = {
    "IBD":  "00002ad2-0000-1000-8000-00805f9b34fb",
    "CP":   "00002ad9-0000-1000-8000-00805f9b34fb",
    "CC02": "8ce5cc02-0a4d-11e9-ab14-d663bd873d93",
    "CC03": "8ce5cc03-0a4d-11e9-ab14-d663bd873d93",
    "EE02": "8ce5ee02-0a4d-11e9-ab14-d663bd873d93",
}


class BleGateway:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()
        self.clients = {}        # address -> BleakClient
        self.names = {}          # address -> name
        self._subs = []          # list[queue.Queue]
        self._lock = threading.Lock()
        self._last_error = ""

    # ---------- 基础设施 ----------
    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def _call(self, coro, timeout=40):
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return fut.result(timeout)

    def emit(self, ev):
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(ev)
            except Exception:
                pass

    def add_sub(self):
        q = queue.Queue(maxsize=5000)
        with self._lock:
            self._subs.append(q)
        return q

    def remove_sub(self, q):
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def _client(self, address):
        return self.clients.get((address or "").upper()) or self.clients.get(address)

    # ---------- 扫描 ----------
    async def _scan(self, timeout):
        from bleak import BleakScanner
        found = await BleakScanner.discover(timeout=float(timeout), return_adv=True)
        out = []
        for addr, pair in found.items():
            dev, adv = pair
            out.append({
                "address": addr,
                "name": dev.name or "",
                "rssi": getattr(adv, "rssi", None),
                "services": list(getattr(adv, "service_uuids", None) or []),
            })
        out.sort(key=lambda d: -(d["rssi"] or -999))
        return out

    def scan(self, timeout=6):
        try:
            return self._call(self._scan(timeout), timeout=float(timeout) + 20)
        except Exception as e:
            self._last_error = f"scan: {e}"
            return []

    # ---------- 连接 ----------
    async def _connect(self, address):
        from bleak import BleakClient
        addr = address
        old = self._client(addr)
        if old is not None:
            try:
                if old.is_connected:
                    await old.disconnect()
            except Exception:
                pass
            self.clients.pop(addr, None)

        def _on_disc(_c):
            self.clients.pop(addr, None)
            self.emit({"type": "disconnected", "address": addr})

        client = BleakClient(addr, disconnected_callback=_on_disc)
        await client.connect()
        self.clients[addr] = client
        try:
            self.names[addr] = client.name or ""
        except Exception:
            self.names[addr] = ""
        return True

    def connect(self, address):
        address = (address or "").strip()
        if not address:
            return {"connected": False, "error": "缺少 address"}
        try:
            self._call(self._connect(address), timeout=45)
            self.emit({"type": "connected", "address": address})
            return {"connected": True, "address": address, "name": self.names.get(address, "")}
        except Exception as e:
            self._last_error = f"connect: {e}"
            return {"connected": False, "address": address, "error": str(e)}

    async def _disconnect(self, address):
        c = self._client(address)
        if c is not None:
            try:
                if c.is_connected:
                    await c.disconnect()
            except Exception:
                pass
            self.clients.pop(address, None)

    def disconnect(self, address=None):
        try:
            if address:
                self._call(self._disconnect(address), timeout=20)
            else:
                for addr in list(self.clients.keys()):
                    self._call(self._disconnect(addr), timeout=20)
        except Exception as e:
            self._last_error = f"disconnect: {e}"
        self.emit({"type": "disconnected", "address": address or ""})
        return {"ok": True}

    # ---------- 特征 ----------
    async def _services(self, address):
        c = self._client(address)
        out = []
        for s in c.services:
            chars = []
            for ch in s.characteristics:
                chars.append({"uuid": str(ch.uuid).lower(), "props": list(ch.properties)})
            out.append({"uuid": str(s.uuid).lower(), "chars": chars})
        return out

    def services(self, address=None):
        addr = address or (list(self.clients.keys())[0] if self.clients else None)
        if not addr or self._client(addr) is None:
            return []
        try:
            return self._call(self._services(addr))
        except Exception as e:
            self._last_error = f"services: {e}"
            return []

    # ---------- 订阅 ----------
    async def _subscribe(self, address, uuid):
        c = self._client(address)

        def cb(sender, data):
            try:
                u = str(sender.uuid).lower()
            except Exception:
                u = uuid
            self.emit({"type": "notify", "address": address, "uuid": u,
                       "data": bytes(data).hex()})

        await c.start_notify(uuid, cb)
        return True

    def subscribe(self, address, uuids):
        if not address or self._client(address) is None:
            return {"ok": False, "error": "未连接"}
        ok, fail = [], []
        for u in uuids:
            try:
                self._call(self._subscribe(address, u), timeout=20)
                ok.append(u)
            except Exception as e:
                fail.append({"uuid": u, "error": str(e)})
        return {"ok": True, "subscribed": ok, "failed": fail}

    # ---------- 读写 ----------
    async def _write(self, address, uuid, data, response):
        c = self._client(address)
        await c.write_gatt_char(uuid, data, response=bool(response))
        return True

    def write(self, address, uuid, hexstr, response=True):
        if not address or self._client(address) is None:
            return {"ok": False, "error": "未连接"}
        try:
            data = bytes.fromhex(hexstr.replace(" ", ""))
            self._call(self._write(address, uuid, data, response), timeout=20)
            return {"ok": True}
        except Exception as e:
            self._last_error = f"write: {e}"
            return {"ok": False, "error": str(e)}

    async def _read(self, address, uuid):
        c = self._client(address)
        return await c.read_gatt_char(uuid)

    def read(self, address, uuid):
        if not address or self._client(address) is None:
            return {"ok": False, "error": "未连接"}
        try:
            data = self._call(self._read(address, uuid), timeout=20)
            return {"ok": True, "data": bytes(data).hex()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ---------- 状态 ----------
    def status(self):
        devs = []
        for addr, c in list(self.clients.items()):
            devs.append({"address": addr, "name": self.names.get(addr, ""),
                         "connected": bool(c.is_connected)})
        return {"connected": len(devs) > 0, "devices": devs, "error": self._last_error}


gateway = BleGateway()
