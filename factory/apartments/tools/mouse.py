"""Mouse & keyboard control - the factory's hands.

Backends, best first:

1. ``je_auto_control`` (AutoControl) - 779 ``AC_*`` commands, template matching,
   OCR and OS accessibility-tree targeting.  Real, on PyPI (v1.0.6), but it
   pulls OpenCV + Pillow, so it is tier-2 and used only when installed.
2. ``pyautogui`` + ``pynput`` - the pragmatic default.  pyautogui for driving,
   pynput for the global kill-switch hotkey.
3. ``ctypes`` user32 (Windows) - zero-dependency fallback: ``SetCursorPos``,
   ``mouse_event``, ``keybd_event``, ``SendInput``.

Every action is permission-gated (see ``permissions/gate.py``):

    move / scroll / screenshot   -> low   (auto)
    click / type / drag / hotkey -> medium (prompt, auto-allow after 10 s)
    anything destructive         -> high/critical (explicit yes, or blocked)

Safety rails that cannot be turned off:
  * **fail-safe corner** - move to (0,0) to abort, and the factory refuses to
    move outside the ``mouse.screen_boundary`` rectangle from allowlist.json.
  * **kill switch** - Ctrl+Shift+Q, installed as a global hotkey, sets the gate's
    kill flag.  It works even while another agent is mid-click.
"""

from __future__ import annotations

import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ..base_apartment import PermissionDenied

#: Action -> risk, used when asking the gate.
MOUSE_RISK = {
    "mouse_move": "low",
    "mouse_scroll": "low",
    "mouse_drag": "medium",
    "mouse_click": "medium",
    "mouse_double_click": "medium",
    "mouse_right_click": "medium",
    "keyboard_type": "medium",
    "keyboard_press": "medium",
    "keyboard_hotkey": "medium",
    "focus_window": "medium",
}

KILL_HOTKEY = ("ctrl", "shift", "q")


class MouseTool:
    """Drives the real mouse and keyboard."""

    def __init__(self, apartment):
        self.apartment = apartment
        self.backend = self._detect_backend()
        self.boundary = self._boundary()
        self.failsafe = True
        self.actions = 0
        self._pynput = None
        if self.backend == "pyautogui":
            try:
                import pyautogui
                pyautogui.FAILSAFE = True
                pyautogui.PAUSE = 0.08
                self._pyautogui = pyautogui
            except Exception:
                self._pyautogui = None
        else:
            self._pyautogui = None

    # --------------------------------------------------------------- backends
    def _detect_backend(self) -> str:
        try:  # pragma: no cover - optional heavy dependency
            import je_auto_control  # noqa: F401
            return "auto_control"
        except Exception:
            pass
        try:
            import pyautogui  # noqa: F401
            return "pyautogui"
        except Exception:
            pass
        if sys.platform == "win32":
            return "user32"
        return "none"

    def _boundary(self) -> Tuple[int, int, int, int]:
        cfg = (self.apartment.gate.allowlist.get("mouse", {}) or {})
        box = cfg.get("screen_boundary") or [0, 0, 1920, 1080]
        try:
            return tuple(int(v) for v in box)[:4]
        except Exception:
            return (0, 0, 1920, 1080)

    def available(self) -> bool:
        return self.backend != "none"

    # ------------------------------------------------------------------ guards
    def _gate(self, action: str, details: Optional[Dict[str, Any]] = None) -> None:
        self.apartment.require("mouse") if hasattr(self.apartment, "require") else None
        self.apartment.gate.enforce(self.apartment.NAME, action, details or {})

    def _inside(self, x: float, y: float) -> bool:
        x0, y0, x1, y1 = self.boundary
        return x0 <= x <= x1 and y0 <= y <= y1

    def _check_point(self, x: float, y: float) -> None:
        if not self._inside(x, y):
            raise PermissionDenied(
                "point (%s,%s) outside allowed screen boundary %s" % (x, y, self.boundary))

    # -------------------------------------------------------------- primitives
    def position(self) -> Tuple[int, int]:
        if self._pyautogui:
            return tuple(self._pyautogui.position())
        if self.backend == "user32":
            return _win_cursor_pos()
        return (0, 0)

    def move(self, x: int, y: int, duration: float = 0.25) -> bool:
        """Move the cursor.  Low risk - auto-approved, but boundary-checked."""
        self._check_point(x, y)
        self._gate("mouse_move", {"x": x, "y": y})
        self.actions += 1
        return self._move(x, y, duration)

    def _move(self, x: int, y: int, duration: float) -> bool:
        if self.backend == "auto_control":
            try:  # pragma: no cover
                import je_auto_control  # type: ignore
                je_auto_control.mouse_move(x, y)
                return True
            except Exception:
                pass
        if self._pyautogui:
            self._pyautogui.moveTo(x, y, duration=max(duration, 0.05))
            return True
        if self.backend == "user32":
            return _win_move(int(x), int(y), duration)
        self.apartment.log("mouse_unavailable", action="move")
        return False

    def click(self, x: Optional[int] = None, y: Optional[int] = None, clicks: int = 1,
              button: str = "left") -> bool:
        """Click at a point (or at the current position).  Medium risk."""
        if x is None or y is None:
            x, y = self.position()
        self._check_point(x, y)
        action = {1: "mouse_click", 2: "mouse_double_click"}.get(clicks, "mouse_click")
        if button == "right":
            action = "mouse_right_click"
        self._gate(action, {"x": x, "y": y, "button": button})
        self.actions += 1
        self._move(x, y, 0.15)
        return self._click(clicks, button)

    def _click(self, clicks: int = 1, button: str = "left") -> bool:
        if self._pyautogui:
            self._pyautogui.click(clicks=clicks, button=button)
            return True
        if self.backend == "user32":
            return _win_click(button, clicks)
        self.apartment.log("mouse_unavailable", action="click")
        return False

    def double_click(self, x: Optional[int] = None, y: Optional[int] = None) -> bool:
        return self.click(x, y, clicks=2)

    def right_click(self, x: Optional[int] = None, y: Optional[int] = None) -> bool:
        return self.click(x, y, button="right")

    def drag(self, start: Tuple[int, int], end: Tuple[int, int], duration: float = 0.5) -> bool:
        self._check_point(*start)
        self._check_point(*end)
        self._gate("mouse_drag", {"from": start, "to": end})
        self.actions += 1
        if self._pyautogui:
            self._pyautogui.moveTo(*start)
            self._pyautogui.dragTo(*end, duration=duration, button="left")
            return True
        if self.backend == "user32":
            return _win_drag(start, end, duration)
        return False

    def scroll(self, clicks: int) -> bool:
        self._gate("mouse_scroll", {"clicks": clicks})
        self.actions += 1
        if self._pyautogui:
            self._pyautogui.scroll(int(clicks))
            return True
        if self.backend == "user32":
            return _win_scroll(int(clicks))
        return False

    # ---------------------------------------------------------------- keyboard
    def type_text(self, text: str, interval: float = 0.02) -> bool:
        """Type a string.  Medium risk - you will be asked first."""
        self._gate("keyboard_type", {"length": len(text), "preview": text[:60]})
        self.actions += 1
        if self._pyautogui:
            self._pyautogui.typewrite(text, interval=interval)
            return True
        if self.backend == "user32":
            return _win_type(text, interval)
        self.apartment.log("mouse_unavailable", action="type")
        return False

    def press(self, key: str) -> bool:
        self._gate("keyboard_press", {"key": key})
        self.actions += 1
        if self._pyautogui:
            self._pyautogui.press(key)
            return True
        if self.backend == "user32":
            return _win_key(key)
        return False

    def hotkey(self, *keys: str) -> bool:
        self._gate("keyboard_hotkey", {"keys": list(keys)})
        self.actions += 1
        if self._pyautogui:
            self._pyautogui.hotkey(*keys)
            return True
        if self.backend == "user32":
            return _win_hotkey(keys)
        return False

    # ----------------------------------------------------------------- windows
    def focus_window(self, title_contains: str) -> bool:
        """Bring a window to the foreground by (partial) title."""
        self._gate("focus_window", {"title": title_contains})
        if sys.platform != "win32":
            return False
        return _win_focus(title_contains)

    # ------------------------------------------------------------ higher level
    def click_text(self, text: str, region: Optional[Tuple[int, int, int, int]] = None) -> bool:
        """OCR the screen, find ``text``, click its centre.

        Needs AutoControl (or pytesseract + pyautogui).  Returns False - never
        guesses - when the text cannot be located.
        """
        screen = self.apartment.tool("screen")
        try:  # pragma: no cover
            import je_auto_control  # type: ignore
            box = je_auto_control.find_text(text=text)
            if box:
                return self.click(int(box[0]), int(box[1]))
        except Exception:
            pass
        try:
            import pytesseract  # type: ignore
            from PIL import Image
            image = screen._grab(region)
            if image is None:
                return False
            data = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT)
            lowered = text.lower()
            for i, word in enumerate(data.get("text", [])):
                if lowered in str(word).lower():
                    x = data["left"][i] + data["width"][i] // 2
                    y = data["top"][i] + data["height"][i] // 2
                    return self.click(x, y)
        except Exception:
            pass
        self.apartment.log("click_text_failed", text=text)
        return False

    def wait_image(self, template: str, timeout_s: float = 10.0,
                   confidence: float = 0.9) -> Optional[Tuple[int, int, int, int]]:
        screen = self.apartment.tool("screen")
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            box = screen.find_on_screen(template, confidence=confidence)
            if box:
                return box
            time.sleep(0.4)
        return None

    # ------------------------------------------------------------ kill switch
    def install_kill_switch(self, on_kill: Optional[Callable[[], None]] = None) -> bool:
        """Register Ctrl+Shift+Q globally.  Returns False if unavailable."""
        try:
            from pynput import keyboard  # type: ignore
        except Exception:
            self.apartment.log("kill_switch_unavailable",
                               reason="pynput not installed; use the CLI 'stop' command")
            return False

        combo = {self._normalise_key(k) for k in KILL_HOTKEY}
        pressed: set = set()
        lock = threading.Lock()

        def handler():
            from ..base_apartment import BaseApartment
            BaseApartment.set_kill_switch(True)
            self.apartment.gate.kill("hotkey Ctrl+Shift+Q")
            print("\n\n  *** KILL SWITCH pressed (Ctrl+Shift+Q) - factory stopped. ***\n")
            if on_kill:
                on_kill()

        def on_press(key):
            with lock:
                pressed.add(self._normalise_key(key))
                if combo.issubset(pressed):
                    handler()

        def on_release(key):
            with lock:
                pressed.discard(self._normalise_key(key))

        listener = keyboard.Listener(on_press=on_press, on_release=on_release)
        listener.daemon = True
        listener.start()
        self._pynput = listener
        return True

    @staticmethod
    def _normalise_key(key) -> str:
        name = getattr(key, "name", None) or str(key)
        name = str(name).replace("Key.", "").replace("'", "").lower()
        return {"ctrl_l": "ctrl", "ctrl_r": "ctrl", "shift_l": "shift", "shift_r": "shift",
                "cmd": "ctrl", "ctrl": "ctrl", "shift": "shift", "q": "q"}.get(name, name)

    def stop_kill_switch(self) -> None:
        if self._pynput is not None:
            try:
                self._pynput.stop()
            except Exception:
                pass
            self._pynput = None

    def info(self) -> Dict[str, Any]:
        return {"backend": self.backend, "boundary": list(self.boundary),
                "failsafe": self.failsafe, "actions": self.actions,
                "kill_switch": self._pynput is not None}


# --------------------------------------------------------------------------- #
# ctypes (Windows) backend - the zero-dependency path
# --------------------------------------------------------------------------- #

def _win_cursor_pos() -> Tuple[int, int]:  # pragma: no cover - Windows only
    import ctypes
    point = ctypes.wintypes.POINT() if hasattr(ctypes, "wintypes") else None
    import ctypes.wintypes as wt
    pt = wt.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
    return (pt.x, pt.y)


def _win_move(x: int, y: int, duration: float) -> bool:  # pragma: no cover
    import ctypes
    steps = max(int(duration / 0.02), 1)
    start = _win_cursor_pos()
    for step in range(1, steps + 1):
        cx = start[0] + (x - start[0]) * step // steps
        cy = start[1] + (y - start[1]) * step // steps
        ctypes.windll.user32.SetCursorPos(int(cx), int(cy))
        time.sleep(0.02)
    ctypes.windll.user32.SetCursorPos(int(x), int(y))
    return True


def _win_click(button: str = "left", clicks: int = 1) -> bool:  # pragma: no cover
    import ctypes
    down, up = {
        "left": (0x0002, 0x0004),
        "right": (0x0008, 0x0010),
        "middle": (0x0020, 0x0040),
    }.get(button, (0x0002, 0x0004))
    for _ in range(max(clicks, 1)):
        ctypes.windll.user32.mouse_event(down, 0, 0, 0, 0)
        time.sleep(0.03)
        ctypes.windll.user32.mouse_event(up, 0, 0, 0, 0)
        time.sleep(0.05)
    return True


def _win_drag(start, end, duration: float) -> bool:  # pragma: no cover
    import ctypes
    ctypes.windll.user32.SetCursorPos(int(start[0]), int(start[1]))
    ctypes.windll.user32.mouse_event(0x0002, 0, 0, 0, 0)
    steps = max(int(duration / 0.03), 1)
    for step in range(1, steps + 1):
        x = start[0] + (end[0] - start[0]) * step // steps
        y = start[1] + (end[1] - start[1]) * step // steps
        ctypes.windll.user32.SetCursorPos(int(x), int(y))
        time.sleep(0.03)
    ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0)
    return True


def _win_scroll(clicks: int) -> bool:  # pragma: no cover
    import ctypes
    ctypes.windll.user32.mouse_event(0x0800, 0, 0, int(clicks) * 120, 0)
    return True


_VK = {**{chr(c): ord(chr(c).upper()) for c in range(97, 123)},
       **{str(d): 48 + d for d in range(10)},
       "enter": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B, "space": 0x20,
       "backspace": 0x08, "delete": 0x2E, "up": 0x26, "down": 0x28, "left": 0x25,
       "right": 0x27, "home": 0x24, "end": 0x23, "ctrl": 0x11, "shift": 0x10,
       "alt": 0x12, "win": 0x5B, "f5": 0x74}


def _win_type(text: str, interval: float) -> bool:  # pragma: no cover
    import ctypes
    for char in text:
        code = _VK.get(char.lower())
        if code is None:
            # Unicode fallback via KEYEVENTF_UNICODE
            for event in (0x0001, 0x0002):   # keydown, keyup
                ctypes.windll.user32.keybd_event(0, ord(char), event | 0x0004, 0)
        else:
            ctypes.windll.user32.keybd_event(code, 0, 0, 0)
            ctypes.windll.user32.keybd_event(code, 0, 0x0002, 0)
        time.sleep(interval)
    return True


def _win_key(key: str) -> bool:  # pragma: no cover
    import ctypes
    code = _VK.get(str(key).lower())
    if code is None:
        return False
    ctypes.windll.user32.keybd_event(code, 0, 0, 0)
    ctypes.windll.user32.keybd_event(code, 0, 0x0002, 0)
    return True


def _win_hotkey(keys: Sequence[str]) -> bool:  # pragma: no cover
    import ctypes
    codes = [_VK.get(str(k).lower()) for k in keys]
    if any(c is None for c in codes):
        return False
    for code in codes:
        ctypes.windll.user32.keybd_event(code, 0, 0, 0)
    for code in reversed(codes):
        ctypes.windll.user32.keybd_event(code, 0, 0x0002, 0)
    return True


def _win_focus(title_contains: str) -> bool:  # pragma: no cover
    import ctypes
    import ctypes.wintypes as wt

    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def enum(hwnd, _):
        if not ctypes.windll.user32.IsWindowVisible(hwnd):
            return True
        length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
        if not length:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
        if title_contains.lower() in buf.value.lower():
            found.append(hwnd)
            return False
        return True

    ctypes.windll.user32.EnumWindows(enum, 0)
    if not found:
        return False
    hwnd = found[0]
    ctypes.windll.user32.ShowWindow(hwnd, 9)      # SW_RESTORE
    ctypes.windll.user32.SetForegroundWindow(hwnd)
    return True


def build(apartment) -> MouseTool:
    return MouseTool(apartment)
