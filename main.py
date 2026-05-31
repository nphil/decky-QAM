import asyncio
import glob
import json
import os
import select
import threading
import time
from collections import deque

import decky

# Valve vendor id. The built-in Steam Deck controller exposes several HID
# interfaces under this vendor; we read them all and key off report length.
VALVE_VID = "28DE"
# DeckState input report is 64 bytes; that is our discriminator.
STATE_LEN = 64
# Digital buttons live in this byte window. Gyro/accel start around byte 16
# and analog sticks/triggers later still, so we stay clear of them.
BUTTON_REGION = range(8, 16)

SETTINGS_FILE = "settings.json"


def _settings_path() -> str:
    return os.path.join(decky.DECKY_PLUGIN_SETTINGS_DIR, SETTINGS_FILE)


def load_settings() -> dict:
    try:
        with open(_settings_path(), "r") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_settings(data: dict) -> None:
    os.makedirs(decky.DECKY_PLUGIN_SETTINGS_DIR, exist_ok=True)
    tmp = _settings_path() + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, _settings_path())


def find_valve_hidraw() -> list[str]:
    """All /dev/hidraw* nodes that belong to a Valve (Steam Deck) device."""
    found = []
    for path in sorted(glob.glob("/dev/hidraw*")):
        name = os.path.basename(path)
        try:
            with open(f"/sys/class/hidraw/{name}/device/uevent", "r") as f:
                data = f.read().upper()
        except OSError:
            continue
        if VALVE_VID in data:
            found.append(path)
    return found


def report_matches(report: bytes, conditions: list[dict]) -> bool:
    if not conditions:
        return False
    for cond in conditions:
        idx = cond["byte"]
        mask = cond["mask"]
        if idx >= len(report) or (report[idx] & mask) != mask:
            return False
    return True


def _region_bits(report: bytes) -> set[int]:
    bits = set()
    for idx in BUTTON_REGION:
        if idx >= len(report):
            break
        byte = report[idx]
        for bit in range(8):
            if byte & (1 << bit):
                bits.add(idx * 8 + bit)
    return bits


class HidListener:
    """
    Reads raw DeckState HID reports from every Valve HID node on a background
    thread. Detects the bound back-button (runtime) and learns a button's bit
    signature via a press-to-bind capture flow.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop):
        self.loop = loop
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

        self.trigger: list[dict] = []
        self.was_pressed = False
        self.devices: list[str] = []

        # diagnostics
        self.stats: dict[str, dict] = {}
        self.recent: deque[str] = deque(maxlen=8)

        # capture state machine
        self._capturing = False
        self._capture_phase = ""
        self._baseline_zero: set[int] | None = None
        self._press_bits: set[int] | None = None
        self._phase_deadline = 0.0
        self._confirm_samples = 0
        self._capture_start = 0.0
        self._capture_reports = 0

    # ---- control ---------------------------------------------------------

    def set_trigger(self, conditions: list[dict]) -> None:
        with self._lock:
            self.trigger = conditions or []
            self.was_pressed = False

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> bool:
        if self.is_running():
            return True
        self.devices = find_valve_hidraw()
        if not self.devices:
            decky.logger.warning("decky-QAM: no Valve HID device found")
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        decky.logger.info("decky-QAM: listening on %s", self.devices)
        return True

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=1.5)
        self._thread = None

    def begin_capture(self) -> None:
        with self._lock:
            self._capturing = True
            self._capture_phase = "baseline"
            self._baseline_zero = None
            self._press_bits = None
            self._confirm_samples = 0
            self._capture_start = time.monotonic()
            self._capture_reports = 0
            self._phase_deadline = time.monotonic() + 1.0

    def cancel_capture(self) -> None:
        with self._lock:
            self._capturing = False
            self._capture_phase = ""

    def diagnostics(self) -> dict:
        return {
            "devices": [
                {
                    "path": p,
                    "reports": self.stats.get(p, {}).get("count", 0),
                    "last_len": self.stats.get(p, {}).get("last_len", 0),
                }
                for p in self.devices
            ],
            "recent": list(self.recent),
            "running": self.is_running(),
        }

    # ---- worker ----------------------------------------------------------

    def _run(self) -> None:
        fdmap: dict[int, str] = {}
        for path in self.devices:
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
                fdmap[fd] = path
                self.stats[path] = {"count": 0, "last_len": 0}
            except OSError as e:
                decky.logger.error("decky-QAM: cannot open %s: %s", path, e)
        if not fdmap:
            return
        try:
            while not self._stop.is_set():
                r, _, _ = select.select(list(fdmap.keys()), [], [], 0.2)
                for fd in r:
                    try:
                        data = os.read(fd, 128)
                    except BlockingIOError:
                        continue
                    except OSError:
                        continue
                    self._record(fdmap[fd], data)
                    self._process(data)
                self._capture_watchdog()
        finally:
            for fd in fdmap:
                try:
                    os.close(fd)
                except OSError:
                    pass

    def _record(self, path: str, data: bytes) -> None:
        s = self.stats.setdefault(path, {"count": 0, "last_len": 0})
        s["count"] += 1
        s["last_len"] = len(data)
        if len(data) >= STATE_LEN and s["count"] % 30 == 1:
            btn = " ".join(f"{b:02x}" for b in data[8:16])
            self.recent.appendleft(
                f"{os.path.basename(path)} len={len(data)} b2={data[2]:02x} btn[8:16]={btn}"
            )

    def _capture_watchdog(self) -> None:
        with self._lock:
            if (
                self._capturing
                and self._capture_reports == 0
                and time.monotonic() - self._capture_start > 3.0
            ):
                self._capturing = False
                self._capture_phase = ""
                self._emit("qam_capture", {"phase": "nodata"})

    def _process(self, data: bytes) -> None:
        if len(data) < STATE_LEN:
            return
        with self._lock:
            if self._capturing:
                self._capture_reports += 1
                self._capture_step(data)
                return
            conditions = self.trigger
        if not conditions:
            return
        pressed = report_matches(data, conditions)
        if pressed and not self.was_pressed:
            self._emit("qam_trigger")
        self.was_pressed = pressed

    def _capture_step(self, report: bytes) -> None:
        now = time.monotonic()
        bits = _region_bits(report)
        all_bits = {idx * 8 + bit for idx in BUTTON_REGION for bit in range(8)}

        if self._capture_phase == "baseline":
            if self._baseline_zero is None:
                self._baseline_zero = all_bits - bits
            else:
                self._baseline_zero -= bits
            if now >= self._phase_deadline:
                self._capture_phase = "wait_press"
                self._phase_deadline = now + 8.0
                self._emit("qam_capture", {"phase": "press"})

        elif self._capture_phase == "wait_press":
            new = bits & (self._baseline_zero or set())
            if new:
                self._press_bits = set(new)
                self._capture_phase = "confirm"
                self._confirm_samples = 0
                self._phase_deadline = now + 0.4
            elif now >= self._phase_deadline:
                self._capturing = False
                self._capture_phase = ""
                self._emit("qam_capture", {"phase": "timeout"})

        elif self._capture_phase == "confirm":
            self._press_bits &= bits
            self._confirm_samples += 1
            if not self._press_bits:
                self._capture_phase = "wait_press"
                self._phase_deadline = now + 8.0
            elif now >= self._phase_deadline and self._confirm_samples >= 3:
                conditions = self._bits_to_conditions(self._press_bits)
                self.trigger = conditions
                self.was_pressed = True
                self._capturing = False
                self._capture_phase = ""
                self._emit("qam_capture", {"phase": "done", "conditions": conditions})

    @staticmethod
    def _bits_to_conditions(bits: set[int]) -> list[dict]:
        by_byte: dict[int, int] = {}
        for b in bits:
            idx, bit = divmod(b, 8)
            by_byte[idx] = by_byte.get(idx, 0) | (1 << bit)
        return [{"byte": idx, "mask": mask} for idx, mask in sorted(by_byte.items())]

    def _emit(self, event: str, payload: dict | None = None) -> None:
        coro = decky.emit(event, payload) if payload is not None else decky.emit(event)
        asyncio.run_coroutine_threadsafe(coro, self.loop)


class Plugin:
    async def _main(self):
        self.loop = asyncio.get_event_loop()
        self.settings = load_settings()
        self.listener = HidListener(self.loop)

        conditions = self.settings.get("trigger") or []
        self.listener.set_trigger(conditions)
        if self.settings.get("enabled", True) and conditions:
            self.listener.start()
        decky.logger.info("decky-QAM loaded")

    async def _unload(self):
        try:
            self.listener.stop()
        except Exception as e:
            decky.logger.error("decky-QAM unload error: %s", e)
        decky.logger.info("decky-QAM unloaded")

    # ---- frontend-callable methods --------------------------------------

    async def get_settings(self) -> dict:
        return {
            "enabled": bool(self.settings.get("enabled", True)),
            "trigger": self.settings.get("trigger") or [],
            "label": self.settings.get("label") or "",
            "device_found": len(find_valve_hidraw()) > 0,
            "running": self.listener.is_running(),
        }

    async def set_enabled(self, enabled: bool) -> dict:
        self.settings["enabled"] = bool(enabled)
        save_settings(self.settings)
        if enabled and self.settings.get("trigger"):
            self.listener.start()
        else:
            self.listener.stop()
        return await self.get_settings()

    async def begin_capture(self) -> bool:
        if not self.listener.start():
            return False
        self.listener.begin_capture()
        return True

    async def cancel_capture(self) -> None:
        self.listener.cancel_capture()

    async def get_diagnostics(self) -> dict:
        self.listener.start()
        await asyncio.sleep(1.2)
        return self.listener.diagnostics()

    async def save_trigger(self, conditions: list, label: str) -> dict:
        self.settings["trigger"] = conditions
        self.settings["label"] = label
        self.settings["enabled"] = True
        save_settings(self.settings)
        self.listener.set_trigger(conditions)
        self.listener.start()
        return await self.get_settings()

    async def clear_trigger(self) -> dict:
        self.settings["trigger"] = []
        self.settings["label"] = ""
        save_settings(self.settings)
        self.listener.set_trigger([])
        return await self.get_settings()
