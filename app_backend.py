"""Windows Desktop Backend for CUKTECH 10 Ultra Monitor."""
import asyncio
import logging
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional

from bleak import BleakScanner

from core.cuktech_ble.controller import (
    AuthConnectionError,
    CHAR_CMD_RECV,
    CHAR_FW_VERSION,
    CuktechBLEController,
)
from core.cuktech_ble.protocol import READABLE_SETTINGS_PIIDS, UUID_FE95
from core.state_protocol_v2 import decode_port
from core.xiaomi_cloud import QrCodeXiaomiCloudClient
from storage import HistoryStore, load_config, save_config

_LOGGER = logging.getLogger("cuktech_monitor")

PORT_NAMES = {1: "c1", 2: "c2", 3: "c3", 4: "a"}
PORT_LABELS = {1: "USB-C1", 2: "USB-C2", 3: "USB-C3", 4: "USB-A"}
PORT_BITS = {"c1": 0, "c2": 1, "c3": 2, "a": 3}

PROTOCOL_SWITCH_BITS = {
    "c1": {"pd": 0, "pps": 1, "ufcs": 2, "_reserved": 3},
    "c2": {"pd": 8, "pps": 9, "ufcs": 10, "_reserved": 11},
    "c3": {"ufcs": 16, "scp": 17},
    "a": {"ufcs": 24, "scp": 25},
}


def _default_port(piid: int) -> Dict[str, Any]:
    return {
        "id": piid,
        "key": PORT_NAMES[piid],
        "label": PORT_LABELS[piid],
        "voltage": 0.0,
        "current": 0.0,
        "power": 0.0,
        "active": False,
        "enabled": True,
        "protocol": "idle",
        "energy_wh": 0.0,
        "peak_w": 0.0,
    }


class CuktechBackend:
    """Manages BLE connection, state, energy integration, and Xiaomi Cloud QR login."""

    def __init__(self) -> None:
        self.config = load_config()
        self.history = HistoryStore()
        self._lock = threading.Lock()

        # Connection state
        self.connected = False
        self.authenticated = False
        self.status_text = "Chưa kết nối"
        self.last_error = ""
        self.device_model = self.config.get("device_model", "")
        self.firmware_version = ""
        self.rssi: Optional[int] = None
        self.connect_time: float = 0.0
        self.last_push_time: float = 0.0

        # Port & settings state
        self.ports: Dict[int, Dict[str, Any]] = {i: _default_port(i) for i in range(1, 5)}
        self.settings: Dict[str, int] = {}
        self.protocol_extend: int = 0
        self._hw_proto: Dict[int, int] = {1: 0, 2: 0, 3: 0, 4: 0}

        # Energy & session tracking
        self._last_energy_ts: Dict[int, Optional[float]] = {i: None for i in range(1, 5)}
        self._last_energy_w: Dict[int, float] = {i: 0.0 for i in range(1, 5)}
        self._active_session_id: Dict[int, Optional[int]] = {i: None for i in range(1, 5)}
        self._last_sample_ts: float = 0.0
        self.chart_history: deque = deque(maxlen=180)  # 3 minutes @ 1Hz

        # BLE loop control
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._reconnect_flag = False
        self._ctrl: Optional[CuktechBLEController] = None
        self._cmd_queue: Optional[asyncio.Queue] = None
        self._decrypt_failures = 0

        # QR login state
        self._qr_client: Optional[QrCodeXiaomiCloudClient] = None
        self._qr_devices: List[Dict[str, Any]] = []

        # Window callbacks (wired by main.py)
        self._on_switch_to_widget = None
        self._on_switch_to_main = None
        self._on_quit = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="ble-worker")
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(lambda: None)

    def _run_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._cmd_queue = asyncio.Queue()
        try:
            self._loop.run_until_complete(self._main_ble_loop())
        except Exception as e:
            _LOGGER.exception("BLE loop crashed: %s", e)
        finally:
            self._loop.close()

    async def _main_ble_loop(self) -> None:
        backoff = 2.0
        while not self._stop_event.is_set():
            mac = (self.config.get("mac") or "").strip()
            token = (self.config.get("token") or "").strip()
            if not mac or not token:
                with self._lock:
                    self.connected = False
                    self.authenticated = False
                    self.status_text = "Chưa cấu hình thiết bị"
                await asyncio.sleep(1.0)
                continue

            self._reconnect_flag = False
            try:
                await self._connect_and_run(mac, token)
                backoff = 2.0
            except AuthConnectionError as e:
                _LOGGER.warning("Auth failed: %s", e)
                with self._lock:
                    self.last_error = f"Xác thực thất bại: {e}"
                    self.status_text = "Lỗi xác thực BLE — thử kết nối lại..."
                backoff = min(backoff * 1.5, 20.0)
            except Exception as e:
                _LOGGER.warning("BLE connection error: %s", e)
                msg = str(e)
                with self._lock:
                    self.last_error = msg
                    self.status_text = f"Mất kết nối ({msg}) — đang thử lại..."
                if "was not found" in msg.lower():
                    backoff = 1.5
                else:
                    backoff = min(backoff * 1.5, 20.0)
            finally:
                await self._cleanup_connection()

            if self._stop_event.is_set():
                break
            for _ in range(int(backoff * 2)):
                if self._stop_event.is_set() or self._reconnect_flag:
                    break
                await asyncio.sleep(0.5)

    async def _cleanup_connection(self) -> None:
        self._close_all_sessions()
        if self._ctrl:
            try:
                if self._ctrl.client and self._ctrl.client.is_connected:
                    await self._ctrl.stop_all_notifications()
                    await asyncio.wait_for(self._ctrl.client.disconnect(), timeout=3.0)
            except Exception:
                pass
            self._ctrl = None
        with self._lock:
            self.connected = False
            self.authenticated = False
            self.connect_time = 0.0
            for i in range(1, 5):
                self.ports[i]["voltage"] = 0.0
                self.ports[i]["current"] = 0.0
                self.ports[i]["power"] = 0.0
                self.ports[i]["active"] = False
                self.ports[i]["protocol"] = "idle"

    async def _connect_and_run(self, mac: str, token: str) -> None:
        with self._lock:
            self.status_text = f"Đang tìm {mac}..."
            self.last_error = ""

        # Quick scan to get RSSI & wake Windows BLE cache
        try:
            devs = await BleakScanner.discover(timeout=3.0, return_adv=True)
            for addr, (d, adv) in devs.items():
                if addr.upper() == mac.upper():
                    with self._lock:
                        self.rssi = adv.rssi
                    break
        except Exception:
            pass

        with self._lock:
            self.status_text = "Đang kết nối Bluetooth..."

        try:
            token_bytes = bytes.fromhex(token)
        except ValueError as e:
            raise AuthConnectionError(f"Token hex không hợp lệ: {e}") from e

        ctrl = CuktechBLEController(mac=mac, token=token_bytes)
        ctrl.on_port_push = self._on_inline_decrypted_push
        self._ctrl = ctrl
        self._decrypt_failures = 0

        await ctrl.connect()
        await asyncio.sleep(1.5)
        await ctrl.read_device_info()

        with self._lock:
            self.device_model = ctrl.device_model or self.device_model
            self.firmware_version = ctrl.firmware_version
            self.status_text = "Đang xác thực MiOT..."

        if not await ctrl.authenticate():
            raise AuthConnectionError("MiOT handshake returned False")

        with self._lock:
            self.connected = True
            self.authenticated = True
            self.connect_time = time.time()
            self.status_text = "Đã kết nối"
            self.last_error = ""

        # Read initial settings & hardware protocol codes
        await self._refresh_settings()

        # Process any frames received during auth
        if ctrl.init_push_frames:
            for frame in ctrl.init_push_frames:
                await self._try_process_inline_frame(frame)
            ctrl.init_push_frames = []

        # Initial poll for all 4 ports (especially C3 & A which don't push automatically)
        for piid in (1, 2, 3, 4):
            await self._poll_port(piid)

        last_c3a_poll = time.time()
        last_settings_refresh = time.time()
        last_keepalive = time.time()
        last_notify = time.time()

        while not self._stop_event.is_set() and not self._reconnect_flag:
            # 1. Process pending user commands
            await self._drain_commands()

            # 2. Wait for BLE notification frame
            try:
                data = await ctrl.wait_notify("cmd_recv", timeout=1.0)
                if data:
                    last_notify = time.time()
                    with self._lock:
                        self.last_push_time = last_notify
                    if len(data) >= 4 and data[2] == 0x02:
                        await ctrl.client.write_gatt_char(
                            CHAR_CMD_RECV, bytes([0x00, 0x00, 0x03, 0x00]), response=False
                        )
                        await self._try_process_inline_frame(data)
                    elif len(data) >= 6 and data[2] == 0x00:
                        await self._handle_multiframe(data)
                    else:
                        await self._try_process_inline_frame(data)
            except asyncio.TimeoutError:
                pass

            now = time.time()

            # 3. Poll C3 & USB-A every 3s (firmware doesn't push them live)
            if now - last_c3a_poll >= 3.0:
                last_c3a_poll = now
                for piid in (3, 4):
                    await self._poll_port(piid)

            # 4. Refresh settings every 45s
            if now - last_settings_refresh >= 45.0:
                last_settings_refresh = now
                await self._refresh_settings()

            # 5. Keepalive every 10s
            if now - last_keepalive >= 10.0:
                last_keepalive = now
                if ctrl.client and ctrl.client.is_connected:
                    try:
                        await ctrl.client.write_gatt_char(
                            CHAR_CMD_RECV, bytes([0x00, 0x00, 0x00, 0x00]), response=False
                        )
                    except Exception:
                        raise ConnectionError("Keepalive write failed")
                else:
                    raise ConnectionError("BLE client disconnected")

            # 6. Watchdog if silent for >45s
            if now - last_notify > 45.0:
                try:
                    await asyncio.wait_for(ctrl.client.read_gatt_char(CHAR_FW_VERSION), timeout=3.0)
                    last_notify = now
                except Exception:
                    raise ConnectionError("BLE link lost (watchdog)")

            # 7. Record 1Hz chart + DB sample
            self._record_tick(now)

    async def _poll_port(self, piid: int) -> None:
        if not self._ctrl:
            return
        try:
            res = await self._ctrl.send_miot_command(2, piid)
            if res and "raw" in res:
                raw_bytes = res["raw"]
                if isinstance(raw_bytes, (bytes, bytearray)) and len(raw_bytes) >= 12:
                    await self._on_inline_decrypted_push(bytes(raw_bytes))
        except Exception as e:
            _LOGGER.debug("Poll port %d failed: %s", piid, e)

    async def _refresh_settings(self) -> None:
        if not self._ctrl:
            return
        new_settings = dict(self.settings)
        for piid in READABLE_SETTINGS_PIIDS:
            try:
                res = await self._ctrl.send_miot_command(2, piid)
                if res and "value" in res and res["value"] is not None:
                    val = int(res["value"])
                    new_settings[str(piid)] = val
                    if piid == 17:
                        val32 = val & 0xFFFFFFFF
                        c1_p = (val32 >> 24) & 0xFF
                        c2_p = (val32 >> 8) & 0xFF
                        if c1_p:
                            self._hw_proto[1] = c1_p
                        if c2_p:
                            self._hw_proto[2] = c2_p
                    elif piid == 18:
                        val32 = val & 0xFFFFFFFF
                        c3_p = (val32 >> 24) & 0xFF
                        a_p = (val32 >> 8) & 0xFF
                        if c3_p:
                            self._hw_proto[3] = c3_p
                        if a_p:
                            self._hw_proto[4] = a_p
                    elif piid == 21:
                        with self._lock:
                            self.protocol_extend = val
            except Exception:
                pass

        port_ctl = new_settings.get("16", 0x0F)
        with self._lock:
            self.settings = new_settings
            for piid in range(1, 5):
                self.ports[piid]["enabled"] = bool(port_ctl & (1 << (piid - 1)))

    async def _try_process_inline_frame(self, raw_data: bytes) -> None:
        if not self._ctrl or len(raw_data) <= 4:
            return
        pt = self._ctrl.decrypt(raw_data[4:])
        if pt and len(pt) >= 8:
            self._decrypt_failures = 0
            await self._on_inline_decrypted_push(pt)
        else:
            self._decrypt_failures += 1
            if self._decrypt_failures >= 8:
                raise ConnectionError("Decrypt failed repeatedly")

    async def _handle_multiframe(self, data: bytes) -> None:
        if not self._ctrl:
            return
        raw_count = data[4] + 0x100 * data[5]
        frame_count = min(raw_count, 32)
        await self._ctrl.client.write_gatt_char(
            CHAR_CMD_RECV, bytes([0x00, 0x00, 0x01, 0x01]), response=False
        )
        payload = b""
        received = 0
        for _ in range(frame_count):
            try:
                frame = await self._ctrl.wait_notify("cmd_recv", timeout=2.5)
            except asyncio.TimeoutError:
                break
            if not frame:
                break
            received += 1
            payload += frame[2:]
        await self._ctrl.client.write_gatt_char(
            CHAR_CMD_RECV, bytes([0x00, 0x00, 0x01, 0x00]), response=False
        )
        if received == raw_count and payload:
            pt = self._ctrl.decrypt(payload)
            if pt and len(pt) >= 8:
                self._decrypt_failures = 0
                await self._on_inline_decrypted_push(pt)

    async def _on_inline_decrypted_push(self, pt: bytes) -> None:
        if len(pt) < 8:
            return
        b4 = pt[4]
        piid = pt[7]

        # Hardware protocol push (PIID 17 / 18)
        if b4 == 0x04 and piid in (17, 18) and len(pt) >= 16:
            val32 = int.from_bytes(pt[12:16], "little") & 0xFFFFFFFF
            hi_p = (val32 >> 24) & 0xFF
            lo_p = (val32 >> 8) & 0xFF
            if piid == 17:
                if hi_p:
                    self._hw_proto[1] = hi_p
                if lo_p:
                    self._hw_proto[2] = lo_p
            else:
                if hi_p:
                    self._hw_proto[3] = hi_p
                if lo_p:
                    self._hw_proto[4] = lo_p
            with self._lock:
                self.settings[str(piid)] = val32
            return

        # Port telemetry push (PIID 1-4)
        if b4 == 0x04 and piid in PORT_NAMES:
            hw_p = self._hw_proto.get(piid, 0) or None
            info = decode_port(piid, pt, hw_protocol=hw_p)
            if not info:
                return
            now = time.time()
            self._update_port_state(piid, info, now)

    def _update_port_state(self, piid: int, info: Dict[str, Any], now: float) -> None:
        voltage = float(info.get("voltage", 0.0))
        current = float(info.get("current", 0.0))
        power = float(info.get("power", 0.0))
        active = bool(info.get("active", False))
        protocol = str(info.get("protocol", "idle"))

        # Trapezoidal energy integration (Wh)
        prev_t = self._last_energy_ts[piid]
        prev_w = self._last_energy_w[piid]
        delta_wh = 0.0
        if prev_t is not None and 0 < (now - prev_t) < 30.0:
            delta_wh = ((prev_w + power) / 2.0) * ((now - prev_t) / 3600.0)
        self._last_energy_ts[piid] = now
        self._last_energy_w[piid] = power

        with self._lock:
            p = self.ports[piid]
            p["voltage"] = round(voltage, 2)
            p["current"] = round(current, 2)
            p["power"] = round(power, 1)
            p["active"] = active
            p["protocol"] = protocol if active else "idle"
            if active:
                p["energy_wh"] = round(p["energy_wh"] + delta_wh, 4)
                if power > p["peak_w"]:
                    p["peak_w"] = round(power, 1)

            # Session lifecycle
            sid = self._active_session_id[piid]
            if active and power >= 0.3:
                if sid is None:
                    p["energy_wh"] = round(delta_wh, 4)
                    p["peak_w"] = round(power, 1)
                    sid = self.history.start_session(piid, PORT_LABELS[piid], protocol)
                    self._active_session_id[piid] = sid
                else:
                    self.history.update_session(sid, p["energy_wh"], p["peak_w"], protocol)
            elif not active and sid is not None:
                self.history.end_session(sid, p["energy_wh"], p["peak_w"])
                self._active_session_id[piid] = None

    def _close_all_sessions(self) -> None:
        with self._lock:
            for piid in range(1, 5):
                sid = self._active_session_id[piid]
                if sid is not None:
                    p = self.ports[piid]
                    self.history.end_session(sid, p["energy_wh"], p["peak_w"])
                    self._active_session_id[piid] = None

    def _record_tick(self, now: float) -> None:
        if now - self._last_sample_ts < 1.0:
            return
        self._last_sample_ts = now
        with self._lock:
            snapshot_ports = {i: dict(self.ports[i]) for i in range(1, 5)}
            total_w = round(sum(p["power"] for p in snapshot_ports.values()), 1)
            self.chart_history.append({
                "ts": round(now, 1),
                "total": total_w,
                "c1": snapshot_ports[1]["power"],
                "c2": snapshot_ports[2]["power"],
                "c3": snapshot_ports[3]["power"],
                "a": snapshot_ports[4]["power"],
            })
        # Persist to SQLite every 3 seconds when any port is active
        if int(now) % 3 == 0 and any(p["active"] for p in snapshot_ports.values()):
            self.history.record_sample(snapshot_ports)

    async def _drain_commands(self) -> None:
        if not self._cmd_queue or not self._ctrl:
            return
        while not self._cmd_queue.empty():
            cmd_type, args, fut = self._cmd_queue.get_nowait()
            try:
                if cmd_type == "toggle_port":
                    piid, enable = args
                    cur = await self._ctrl.send_miot_command(2, 16)
                    mask = cur["value"] if (cur and isinstance(cur.get("value"), int)) else self.settings.get("16", 0x0F)
                    bit = piid - 1
                    new_mask = (mask | (1 << bit)) if enable else (mask & ~(1 << bit))
                    res = await self._ctrl.send_miot_command(2, 16, value=new_mask)
                    if res:
                        with self._lock:
                            self.settings["16"] = new_mask
                            self.ports[piid]["enabled"] = bool(enable)
                            if not enable:
                                self.ports[piid]["voltage"] = 0.0
                                self.ports[piid]["current"] = 0.0
                                self.ports[piid]["power"] = 0.0
                                self.ports[piid]["active"] = False
                                self.ports[piid]["protocol"] = "idle"
                        fut.set_result({"ok": True})
                    else:
                        fut.set_result({"ok": False, "error": "Thiết bị không phản hồi"})
                elif cmd_type == "set_piid":
                    piid, value = args
                    res = await self._ctrl.send_miot_command(2, int(piid), value=int(value))
                    if res:
                        with self._lock:
                            self.settings[str(piid)] = int(value)
                            if int(piid) == 21:
                                self.protocol_extend = int(value)
                        fut.set_result({"ok": True})
                    else:
                        fut.set_result({"ok": False, "error": "Thiết bị từ chối lệnh"})
            except Exception as e:
                if not fut.done():
                    fut.set_result({"ok": False, "error": str(e)})

    def _submit_cmd(self, cmd_type: str, args: Any, timeout: float = 8.0) -> Dict[str, Any]:
        if not self._loop or not self._cmd_queue or not self.authenticated:
            return {"ok": False, "error": "Chưa kết nối tới củ sạc"}
        fut = self._loop.create_future()

        def _enqueue():
            self._cmd_queue.put_nowait((cmd_type, args, fut))

        # Thread-safe future wait via concurrent.futures
        import concurrent.futures

        cfut = concurrent.futures.Future()

        async def _run_wait():
            await self._cmd_queue.put((cmd_type, args, fut))
            try:
                res = await asyncio.wait_for(fut, timeout=timeout)
                cfut.set_result(res)
            except Exception as ex:
                cfut.set_result({"ok": False, "error": str(ex)})

        asyncio.run_coroutine_threadsafe(_run_wait(), self._loop)
        return cfut.result(timeout=timeout + 1.0)

    # ── Public API for PyWebView ──

    def get_snapshot(self) -> Dict[str, Any]:
        with self._lock:
            ports_list = [dict(self.ports[i]) for i in range(1, 5)]
            total_w = round(sum(p["power"] for p in ports_list), 1)
            active_count = sum(1 for p in ports_list if p["active"])
            v = self.protocol_extend
            switches = {
                "c1": {"pd": bool(v & (1 << 0)), "pps": bool(v & (1 << 1)), "ufcs": bool(v & (1 << 2))},
                "c2": {"pd": bool(v & (1 << 8)), "pps": bool(v & (1 << 9)), "ufcs": bool(v & (1 << 10))},
                "c3": {"ufcs": bool(v & (1 << 16)), "scp": bool(v & (1 << 17))},
                "a": {"ufcs": bool(v & (1 << 24)), "scp": bool(v & (1 << 25))},
            }
            return {
                "connected": self.connected,
                "authenticated": self.authenticated,
                "status_text": self.status_text,
                "last_error": self.last_error,
                "device_name": self.config.get("device_name", "CUKTECH 10 Ultra"),
                "device_model": self.device_model,
                "firmware_version": self.firmware_version,
                "mac": self.config.get("mac", ""),
                "has_config": bool(self.config.get("mac") and self.config.get("token")),
                "rssi": self.rssi,
                "uptime_sec": int(time.time() - self.connect_time) if self.connected and self.connect_time else 0,
                "total_power": total_w,
                "max_power": 120.0,
                "active_ports": active_count,
                "today_wh": self.history.get_today_energy_wh(),
                "ports": ports_list,
                "settings": dict(self.settings),
                "protocol_switches": switches,
                "chart": list(self.chart_history),
                "auto_start": self.is_auto_start_enabled(),
            }

    def is_auto_start_enabled(self) -> bool:
        if sys.platform != "win32":
            return False
        try:
            import winreg
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run",
                0,
                winreg.KEY_READ,
            ) as key:
                winreg.QueryValueEx(key, "CuktechMonitor")
                return True
        except FileNotFoundError:
            return False
        except Exception:
            return False

    def set_auto_start(self, enabled: bool) -> Dict[str, Any]:
        if sys.platform != "win32":
            return {"ok": False, "error": "Chỉ hỗ trợ Windows"}
        try:
            import winreg
            key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
                if enabled:
                    if getattr(sys, "frozen", False):
                        cmd = f'"{sys.executable}" --widget'
                    else:
                        app_dir = Path(__file__).resolve().parent
                        pythonw = app_dir / ".venv" / "Scripts" / "pythonw.exe"
                        main_py = app_dir / "main.py"
                        cmd = f'"{pythonw}" "{main_py}" --widget'
                    winreg.SetValueEx(key, "CuktechMonitor", 0, winreg.REG_SZ, cmd)
                else:
                    try:
                        winreg.DeleteValue(key, "CuktechMonitor")
                    except FileNotFoundError:
                        pass
            return {"ok": True, "enabled": bool(enabled)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def switch_to_widget(self) -> None:
        if self._on_switch_to_widget:
            self._on_switch_to_widget()

    def switch_to_main(self) -> None:
        if self._on_switch_to_main:
            self._on_switch_to_main()

    def quit_app(self) -> None:
        if self._on_quit:
            self._on_quit()

    def toggle_port(self, piid: int, enable: bool) -> Dict[str, Any]:
        return self._submit_cmd("toggle_port", (int(piid), bool(enable)))

    def set_setting(self, piid: int, value: int) -> Dict[str, Any]:
        return self._submit_cmd("set_piid", (int(piid), int(value)))

    def set_protocol_switch(self, port_key: str, proto_key: str, enable: bool) -> Dict[str, Any]:
        bits = PROTOCOL_SWITCH_BITS.get(port_key, {})
        if proto_key not in bits:
            return {"ok": False, "error": "Invalid protocol key"}
        bit = bits[proto_key]
        cur = self.protocol_extend
        # Ensure reserved bit 3 / 11 stays 1 for C1/C2
        if port_key == "c1":
            cur |= (1 << 3)
        elif port_key == "c2":
            cur |= (1 << 11)
        new_val = (cur | (1 << bit)) if enable else (cur & ~(1 << bit))
        return self._submit_cmd("set_piid", (21, new_val))

    def save_device_config(self, mac: str, token: str, ble_key: str = "", name: str = "CUKTECH 10 Ultra") -> Dict[str, Any]:
        self.config = save_config({
            **self.config,
            "mac": mac.strip().upper(),
            "token": token.strip(),
            "ble_key": ble_key.strip(),
            "device_name": name.strip() or "CUKTECH 10 Ultra",
        })
        self._reconnect_flag = True
        return {"ok": True}

    def reconnect(self) -> Dict[str, Any]:
        self._reconnect_flag = True
        return {"ok": True}

    def scan_ble_devices(self) -> Dict[str, Any]:
        async def _scan():
            found = []
            devs = await BleakScanner.discover(timeout=4.0, return_adv=True)
            for addr, (d, adv) in devs.items():
                name = d.name or adv.local_name or ""
                has_fe95 = UUID_FE95 in adv.service_data or any("fe95" in k.lower() for k in adv.service_data)
                if has_fe95 or "cuk" in name.lower() or "mi " in name.lower() or "ad1204" in name.lower():
                    found.append({
                        "mac": addr.upper(),
                        "name": name or "Xiaomi/Cuktech BLE",
                        "rssi": adv.rssi,
                        "fe95": has_fe95,
                    })
            found.sort(key=lambda x: x["rssi"], reverse=True)
            return found

        try:
            loop = asyncio.new_event_loop()
            devices = loop.run_until_complete(_scan())
            loop.close()
            return {"ok": True, "devices": devices}
        except Exception as e:
            return {"ok": False, "error": str(e), "devices": []}

    def start_qr_login(self, server: str = "cn") -> Dict[str, Any]:
        try:
            self._qr_client = QrCodeXiaomiCloudClient(server=server)
            res = self._qr_client.start_qr_login()
            return {"ok": True, "qr_image": res.get("qr_image"), "login_url": res.get("login_url")}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def poll_qr_login(self) -> Dict[str, Any]:
        if not self._qr_client:
            return {"ok": False, "status": "idle", "error": "Chưa bắt đầu đăng nhập QR"}
        if not self._qr_client._poll_done:
            return {"ok": True, "status": "waiting"}
        if self._qr_client._poll_error:
            return {"ok": False, "status": "error", "error": self._qr_client._poll_error}
        try:
            ok = self._qr_client.complete_qr_login()
            if not ok:
                return {"ok": False, "status": "error", "error": "Hoàn tất đăng nhập thất bại"}
            raw_devices = self._qr_client.get_devices()
            devices = []
            for d in raw_devices:
                model = getattr(d, "model", "") or ""
                mac = getattr(d, "mac", "") or ""
                token = getattr(d, "token", "") or ""
                did = getattr(d, "did", "") or ""
                name = getattr(d, "name", "") or model
                if not mac:
                    continue
                ble_key = ""
                if "cuk" in model.lower() or "ad1204" in model.lower() or "fitting" in model.lower():
                    try:
                        ble_key = self._qr_client.get_beaconkey(did, timeout=8.0) or ""
                    except Exception:
                        pass
                devices.append({
                    "did": did,
                    "name": name,
                    "model": model,
                    "mac": mac.upper(),
                    "token": token,
                    "ble_key": ble_key,
                    "is_cuktech": ("cuk" in model.lower() or "ad1204" in model.lower()),
                })
            devices.sort(key=lambda x: (not x["is_cuktech"], x["name"]))
            self._qr_devices = devices
            return {"ok": True, "status": "done", "devices": devices}
        except Exception as e:
            return {"ok": False, "status": "error", "error": str(e)}

    def get_sessions(self) -> List[Dict[str, Any]]:
        return self.history.get_recent_sessions(40)

    def export_csv(self) -> Dict[str, Any]:
        try:
            csv_text = self.history.export_csv()
            from storage import DATA_DIR
            out_path = DATA_DIR / f"cuktech_export_{int(time.time())}.csv"
            out_path.write_text(csv_text, encoding="utf-8")
            return {"ok": True, "path": str(out_path)}
        except Exception as e:
            return {"ok": False, "error": str(e)}
