import asyncio
import glob
import json
import os
import select
import threading
import time

import decky

# Valve vendor id; Steam Deck (LCD + OLED) built-in controller product id.
VALVE_VID = "28DE"
DECK_PIDS = ("1205",)
REPORT_LEN = 64
# DeckState input report carries the button bits. Other report types
# (haptics acks, etc.) are ignored so the bit layout stays consistent.
DECK_STATE_REPORT_TYPE = 0x09

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


def find_deck_hidraw() -> str | None:
    """Locate the /dev/hidraw node for the built-in Steam Deck controller."""
    for path in sorted(glob.glob("/dev/hidraw*")):
        name = os.path.basename(path)
        uevent = f"/sys/class/hidraw/{name}/device/uevent"
        try:
            with open(uevent, "r") as f:
                data = f.read().upper()
        except OSError:
            continue
        if VALVE_VID in data and any(pid in data for pid in DECK_PIDS):
            return path
    return None


def report_matches(report: bytes, conditions: list[dict]) -> bool:
    """True when every (byte, mask) bit in the saved trigger is set."""
    if not conditions:
        return False
    for cond in conditions:
        idx = cond["byte"]
        mask = cond["mask"]
        if idx >= len(report) or (report[idx] & mask) != mask:
            return False
    return True


class HidListener:
    """
    Reads raw DeckState HID reports on a background thread.

    Two jobs:
      * runtime  - watch for the bound back-button signature and, on a
                   rising edge, ask the frontend to open the QAM.
      * capture  - "learn" the bit signature of whichever button the user
                   presses, so we never have to hard-code per-revision
                   bit offsets.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop):
        self.loop = loop
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

        self.trigger: list[dict] = []
        self.was_pressed = False
        self.device_path: str | None = None

        # capture state machine
        self._capturing = False
        self._capture_phase = ""        # "baseline" | "wait_press" | "confirm"
        self._baseline_zero: set[int] | None = None  # bit indexes that stayed 0
        self._press_bits: set[int] | None = None
        self._phase_deadline = 0.0
        self._capture_samples = 0

    # ---- public control --------------------------------------------------

    def set_trigger(self, conditions: list[dict]) -> None:
        with self._lock:
            self.trigger = conditions or []
            self.was_pressed = False

    def start(self) -> bool:
        if self._thread and self._thread.is_alive():
            return True
        self.device_path = find_deck_hidraw()
        if not self.device_path:
            decky.logger.warning("decky-QAM: no Steam Deck hidraw device found")
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        decky.logger.info("decky-QAM: listening on %s", self.device_path)
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
            self._capture_samples = 0
            self._phase_deadline = time.monotonic() + 1.0

    def cancel_capture(self) -> None:
        with self._lock:
            self._capturing = False
            self._capture_phase = ""

    # ---- worker ----------------------------------------------------------

    def _run(self) -> None:
        try:
            fd = os.open(self.device_path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError as e:
            decky.logger.error("decky-QAM: cannot open %s: %s", self.device_path, e)
            return
        try:
            while not self._stop.is_set():
                r, _, _ = select.select([fd], [], [], 0.2)
                if not r:
                    continue
                try:
                    report = os.read(fd, REPORT_LEN)
                except BlockingIOError:
                    continue
                except OSError:
                    break
                if len(report) < REPORT_LEN:
                    continue
                # Keep only the button-bearing DeckState report.
                if report[2] != DECK_STATE_REPORT_TYPE:
                    continue
                self._handle_report(report)
        finally:
            os.close(fd)

    def _handle_report(self, report: bytes) -> None:
        with self._lock:
            if self._capturing:
                self._capture_step(report)
                return
            conditions = self.trigger

        if not conditions:
            return
        pressed = report_matches(report, conditions)
        if pressed and not self.was_pressed:
            self._emit("qam_trigger")
        self.was_pressed = pressed

    # Button bits live in a fixed window of the DeckState report. We diff
    # against an idle baseline rather than trusting hard-coded offsets, but
    # restrict to the button region so analog/gyro noise can't leak in.
    BUTTON_REGION = range(8, 14)

    def _report_bits(self, report: bytes) -> set[int]:
        bits = set()
        for idx in self.BUTTON_REGION:
            byte = report[idx]
            for bit in range(8):
                if byte & (1 << bit):
                    bits.add(idx * 8 + bit)
        return bits

    def _capture_step(self, report: bytes) -> None:
        now = time.monotonic()
        bits = self._report_bits(report)

        if self._capture_phase == "baseline":
            # Bits that are 0 across the whole "hold still" window.
            all_bits = {idx * 8 + bit for idx in self.BUTTON_REGION for bit in range(8)}
            if self._baseline_zero is None:
                self._baseline_zero = all_bits - bits
            else:
                self._baseline_zero -= bits
            if now >= self._phase_deadline:
                self._capture_phase = "wait_press"
                self._phase_deadline = now + 8.0  # user has 8s to press
                self._emit("qam_capture", {"phase": "press"})

        elif self._capture_phase == "wait_press":
            new = bits & (self._baseline_zero or set())
            if new:
                self._press_bits = set(new)
                self._capture_phase = "confirm"
                self._capture_samples = 0
                self._phase_deadline = now + 0.4
            elif now >= self._phase_deadline:
                self._capturing = False
                self._capture_phase = ""
                self._emit("qam_capture", {"phase": "timeout"})

        elif self._capture_phase == "confirm":
            # Keep only bits held high for the whole confirm window.
            self._press_bits &= bits
            self._capture_samples += 1
            if not self._press_bits:
                # released too soon / noise - go back to waiting
                self._capture_phase = "wait_press"
                self._phase_deadline = now + 8.0
            elif now >= self._phase_deadline and self._capture_samples >= 3:
                conditions = self._bits_to_conditions(self._press_bits)
                self.trigger = conditions
                self.was_pressed = True  # avoid instant re-fire on release edge
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
        device = find_deck_hidraw()
        return {
            "enabled": bool(self.settings.get("enabled", True)),
            "trigger": self.settings.get("trigger") or [],
            "label": self.settings.get("label") or "",
            "device_found": device is not None,
            "running": bool(self.listener._thread and self.listener._thread.is_alive()),
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
        # Listener must be running to read reports during capture.
        if not (self.listener._thread and self.listener._thread.is_alive()):
            if not self.listener.start():
                return False
        self.listener.begin_capture()
        return True

    async def cancel_capture(self) -> None:
        self.listener.cancel_capture()

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
        self.listener.stop()
        return await self.get_settings()
