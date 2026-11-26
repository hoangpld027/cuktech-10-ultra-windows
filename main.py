"""CUKTECH 10 Ultra Windows Monitor — Native Desktop & Liquid Glass Widget Entrypoint."""
import ctypes
from ctypes import wintypes
import logging
import os
import sys
from pathlib import Path

os.environ.setdefault("PYTHONUTF8", "1")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

import webview

from app_backend import CuktechBackend

APP_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
EXEC_DIR = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
DATA_DIR = EXEC_DIR / "data"
UI_INDEX = APP_DIR / "ui" / "index.html"
UI_WIDGET = APP_DIR / "ui" / "widget.html"

WIDGET_W = 396
WIDGET_H = 124


def _setup_logging() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    handlers = [
        logging.FileHandler(DATA_DIR / "app.log", encoding="utf-8"),
    ]
    if sys.stderr is not None:
        try:
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
        handlers.append(logging.StreamHandler(sys.stderr))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
    )


def _get_widget_position(width: int = WIDGET_W, height: int = WIDGET_H) -> tuple:
    """Position widget cleanly at bottom-right above the Windows taskbar."""
    try:
        class RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]
        rect = RECT()
        ctypes.windll.user32.SystemParametersInfoW(48, 0, ctypes.byref(rect), 0)
        return max(16, rect.right - width - 24), max(16, rect.bottom - height - 24)
    except Exception:
        return 40, 40


def _apply_windows_liquid_glass(window_title: str, width: int, height: int, radius: int = 24) -> None:
    """Apply true Windows 11 Acrylic blur + clean rounded region clipping (no rectangular halo)."""
    if sys.platform != "win32":
        return
    try:
        user32 = ctypes.windll.user32
        dwmapi = ctypes.windll.dwmapi
        gdi32 = ctypes.windll.gdi32

        hwnd = user32.FindWindowW(None, window_title)
        if not hwnd:
            return

        # 1. Windows 11 DWM rounded corners (DWMWA_WINDOW_CORNER_PREFERENCE = 33, DWMWCP_ROUND = 2)
        corner_pref = ctypes.c_int(2)
        dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(corner_pref), ctypes.sizeof(corner_pref))

        # 2. Enable native Acrylic blur behind the window via SetWindowCompositionAttribute
        class ACCENT_POLICY(ctypes.Structure):
            _fields_ = [
                ("AccentState", ctypes.c_int),
                ("AccentFlags", ctypes.c_int),
                ("GradientColor", ctypes.c_uint),
                ("AnimationId", ctypes.c_int),
            ]

        class WINDOWCOMPOSITIONATTRIBDATA(ctypes.Structure):
            _fields_ = [
                ("Attribute", ctypes.c_int),
                ("Data", ctypes.POINTER(ACCENT_POLICY)),
                ("SizeOfData", ctypes.c_size_t),
            ]

        # AccentState=4 (ACCENT_ENABLE_ACRYLICBLURBEHIND), GradientColor=AABBGGRR
        accent = ACCENT_POLICY(4, 2, 0x26281E14, 0)
        data = WINDOWCOMPOSITIONATTRIBDATA(19, ctypes.pointer(accent), ctypes.sizeof(accent))
        user32.SetWindowCompositionAttribute(hwnd, ctypes.byref(data))
    except Exception as e:
        logging.getLogger("cuktech_monitor").debug("Liquid glass DWM hook skipped: %s", e)


def main() -> None:
    _setup_logging()
    start_in_widget = "--widget" in sys.argv

    backend = CuktechBackend()
    backend.start()

    wx, wy = _get_widget_position(WIDGET_W, WIDGET_H)

    main_win = webview.create_window(
        title="CUKTECH 10 Ultra Monitor",
        url=UI_INDEX.as_uri(),
        js_api=backend,
        width=460,
        height=820,
        min_size=(400, 680),
        background_color="#070b12",
        hidden=start_in_widget,
    )

    widget_win = webview.create_window(
        title="CUKTECH Widget",
        url=UI_WIDGET.as_uri(),
        js_api=backend,
        width=WIDGET_W,
        height=WIDGET_H,
        x=wx,
        y=wy,
        resizable=False,
        frameless=True,
        easy_drag=True,
        shadow=False,
        on_top=True,
        transparent=True,
        hidden=not start_in_widget,
    )

    def _style_widget():
        _apply_windows_liquid_glass("CUKTECH Widget", WIDGET_W, WIDGET_H, radius=24)

    widget_win.events.shown += _style_widget

    def switch_to_widget():
        main_win.hide()
        widget_win.show()
        _style_widget()

    def switch_to_main():
        widget_win.hide()
        main_win.show()

    def quit_app():
        backend.stop()
        try:
            widget_win.destroy()
        except Exception:
            pass
        try:
            main_win.destroy()
        except Exception:
            pass
        os._exit(0)

    backend._on_switch_to_widget = switch_to_widget
    backend._on_switch_to_main = switch_to_main
    backend._on_quit = quit_app

    def on_main_closed():
        backend.stop()
        try:
            widget_win.destroy()
        except Exception:
            pass
        os._exit(0)

    main_win.events.closed += on_main_closed
    webview.start(debug=False)


if __name__ == "__main__":
    main()
