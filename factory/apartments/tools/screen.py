"""Screen capture + on-screen search, with three backends.

Backends, best first:
    1. ``je_auto_control`` (AutoControl) - template matching + OCR + a11y tree,
       when you installed it (it pulls OpenCV).
    2. ``pillow`` / ``pyautogui`` - plain screenshots, no OCR.
    3. stdlib ``ctypes`` on Windows (GDI bitblt) - zero dependencies.

Screenshots are written into the calling apartment's sandbox, never elsewhere,
and every capture is logged (it is a low-risk action, but it is still an
observation of your screen, so the audit trail records it).
"""

from __future__ import annotations

import io
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


class ScreenTool:
    """Capture the screen and find things on it."""

    def __init__(self, apartment):
        self.apartment = apartment
        self.backend = self._detect_backend()
        self.shots = 0

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
        try:
            from PIL import ImageGrab  # noqa: F401
            return "pillow"
        except Exception:
            pass
        if sys.platform == "win32":
            return "gdi"
        if _which("scrot") or _which("import"):
            return "cli"
        return "none"

    # --------------------------------------------------------------- captures
    def screenshot(self, filename: Optional[str] = None, region: Optional[Tuple[int, int, int, int]] = None
                   ) -> Optional[Path]:
        """Save a screenshot into the apartment sandbox.  Returns its path."""
        self.apartment.gate.enforce(self.apartment.NAME, "screen_capture",
                                    {"path": filename or "shot.png"})
        name = filename or ("screen_%s.png" % time.strftime("%Y%m%d_%H%M%S"))
        if not name.endswith((".png", ".jpg", ".jpeg")):
            name += ".png"
        target = self.apartment.sandbox_path("shots/" + name)
        target.parent.mkdir(parents=True, exist_ok=True)

        image = self._grab(region)
        if image is None:
            self.apartment.log("screenshot_failed", backend=self.backend)
            return None
        image.save(target)
        self.shots += 1
        self.apartment.log("screenshot", file=name, backend=self.backend)
        return target

    def _grab(self, region=None):
        """Return a PIL Image, or ``None`` when no backend can do it."""
        if self.backend in ("pyautogui", "pillow", "auto_control"):
            try:
                from PIL import ImageGrab
                return ImageGrab.grab(bbox=region)
            except Exception:
                pass
        if self.backend == "gdi":
            return _gdi_capture(region)
        if self.backend == "cli":
            return _cli_capture(region)
        return None

    def size(self) -> Tuple[int, int]:
        """Primary monitor size - used to keep the mouse inside the boundary."""
        if self.backend == "pyautogui":
            try:
                import pyautogui
                return tuple(pyautogui.size())
            except Exception:
                pass
        if sys.platform == "win32":
            try:
                import ctypes
                return (ctypes.windll.user32.GetSystemMetrics(0),
                        ctypes.windll.user32.GetSystemMetrics(1))
            except Exception:
                pass
        return (1920, 1080)

    # ----------------------------------------------------------------- search
    def find_on_screen(self, template_path: str, confidence: float = 0.9,
                       region: Optional[Tuple[int, int, int, int]] = None
                       ) -> Optional[Tuple[int, int, int, int]]:
        """Locate a template image.  Needs OpenCV (via AutoControl) or pyautogui."""
        try:  # pragma: no cover - optional dependency
            if self.backend == "auto_control":
                import je_auto_control  # type: ignore
                hit = je_auto_control.find_image(image=template_path, confidence=confidence)
                if hit:
                    return tuple(hit)
        except Exception:
            pass
        try:
            import pyautogui
            box = pyautogui.locateOnScreen(template_path, confidence=max(confidence, 0.5),
                                           region=region)
            return tuple(box) if box else None
        except Exception:
            return None

    def read_text(self, region=None) -> str:
        """OCR the screen.  Only works when AutoControl (or pytesseract) is present."""
        try:  # pragma: no cover
            import je_auto_control  # type: ignore
            result = je_auto_control.read_screen_text()
            if result:
                return str(result)
        except Exception:
            pass
        try:
            import pytesseract  # type: ignore
            from PIL import Image
            image = self._grab(region)
            return pytesseract.image_to_string(image) if image else ""
        except Exception:
            return ""

    def window_titles(self) -> List[str]:
        """Titles of visible windows (Windows only; empty elsewhere)."""
        if sys.platform != "win32":
            return []
        titles: List[str] = []

        def _enum(hwnd, _):
            if _is_window_visible(hwnd):
                titles.append(_window_title(hwnd))
            return True

        try:
            import ctypes
            EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
            ctypes.windll.user32.EnumWindows(EnumWindowsProc(_enum), 0)
        except Exception:
            pass
        return [t for t in titles if t]

    def shot_count(self) -> int:
        return self.shots


# --------------------------------------------------------------------------- #
# Backend helpers
# --------------------------------------------------------------------------- #

def _which(name: str) -> Optional[str]:
    from shutil import which
    return which(name)


def _gdi_capture(region=None):  # pragma: no cover - Windows only
    """Screenshot via raw GDI (BitBlt) - no dependencies at all."""
    import ctypes
    from ctypes import wintypes

    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
    if region:
        x, y, w, h = region
    else:
        x, y = 0, 0
        w = user32.GetSystemMetrics(0)
        h = user32.GetSystemMetrics(1)
    hdc_screen = user32.GetDC(0)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
    hbmp = gdi32.CreateCompatibleBitmap(hdc_screen, w, h)
    gdi32.SelectObject(hdc_mem, hbmp)
    SRCCOPY = 0x00CC0020
    gdi32.BitBlt(hdc_mem, 0, 0, w, h, hdc_screen, x, y, SRCCOPY)

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                    ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                    ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                    ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD)]

    bi = BITMAPINFOHEADER()
    bi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bi.biWidth, bi.biHeight = w, -h
    bi.biPlanes, bi.biBitCount = 1, 32
    bi.biCompression = 0
    buffer_size = w * h * 4
    buffer = ctypes.create_string_buffer(buffer_size)
    gdi32.GetDIBits(hdc_mem, hbmp, 0, h, buffer, ctypes.byref(bi), 0)

    try:
        from PIL import Image
        image = Image.frombuffer("RGB", (w, h), buffer, "raw", "BGRX", 0, 1)
        return image
    except Exception:
        # No PIL: write a raw BMP by hand so the file is still inspectable.
        header = b"BM" + (buffer_size + 54).to_bytes(4, "little") + b"\x00\x00\x00\x00" + \
            (54).to_bytes(4, "little") + (40).to_bytes(4, "little") + \
            w.to_bytes(4, "little", signed=True) + h.to_bytes(4, "little", signed=True) + \
            (1).to_bytes(2, "little") + (32).to_bytes(2, "little") + (0).to_bytes(4, "little")
        return _RawImage(header, buffer, (w, h))
    finally:
        gdi32.DeleteObject(hbmp)
        gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(0, hdc_screen)


class _RawImage:  # pragma: no cover - Windows-only fallback
    """Minimal stand-in with a ``save()`` method, for the no-PIL case."""

    def __init__(self, header: bytes, pixels: bytes, size: Tuple[int, int]):
        self.header, self.pixels, self.size = header, pixels, size

    def save(self, path) -> None:
        with open(path, "wb") as fh:
            fh.write(self.header)
            fh.write(self.pixels)


def _cli_capture(region=None):  # pragma: no cover - Linux only
    import tempfile
    from shutil import which
    tmp = Path(tempfile.gettempdir()) / "clienthunter_shot.png"
    if which("scrot"):
        cmd = ["scrot", "-o", str(tmp)]
        if region:
            x, y, w, h = region
            cmd = ["scrot", "-o", "-a", "%d,%d,%d,%d" % (x, y, w, h), str(tmp)]
    elif which("import"):
        cmd = ["import", "-window", "root", str(tmp)]
    else:
        return None
    try:
        subprocess.run(cmd, timeout=15, check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        return None
    try:
        from PIL import Image
        return Image.open(tmp).copy()
    except Exception:
        return None


def _is_window_visible(hwnd) -> bool:  # pragma: no cover
    import ctypes
    return bool(ctypes.windll.user32.IsWindowVisible(hwnd))


def _window_title(hwnd) -> str:  # pragma: no cover
    import ctypes
    length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
    if not length:
        return ""
    buffer = ctypes.create_unicode_buffer(length + 1)
    ctypes.windll.user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value


def build(apartment) -> ScreenTool:
    return ScreenTool(apartment)
