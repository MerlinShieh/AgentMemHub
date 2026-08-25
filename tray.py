# -*- coding: utf-8 -*-
"""Windows system tray for AI Conversation Hub.

The packaged app runs this component in-process, so the tray always knows the
actual HTTP port and can shut down the whole app. Source users may still run
this file directly; that fallback discovers or starts the local server.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

from app_paths import DATA_DIR, RESOURCE_DIR


if os.name != "nt":
    raise RuntimeError("The tray component is available on Windows only")


SOURCE_DIR = Path(__file__).resolve().parent
INSTANCE_PATH = DATA_DIR / "instance.json"
DEFAULT_URL = "http://127.0.0.1:8765"
DETACHED_PROCESS = 0x00000008
ERROR_ALREADY_EXISTS = 183

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
shell32 = ctypes.windll.shell32

user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.DefWindowProcW.restype = ctypes.c_ssize_t

WM_USER = 0x0400
WM_TRAYICON = WM_USER + 20
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_TIMER = 0x0113
WM_DESTROY = 0x0002
TIMER_OPEN = 1
TIMER_WATCHDOG = 2
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 1, 2, 4
TPM_RIGHTALIGN, TPM_RETURNCMD, TPM_NONOTIFY = 0x0008, 0x0100, 0x0040
MF_SEPARATOR, MF_CHECKED, MF_UNCHECKED = 0x0800, 0x0008, 0x0000
IDI_APPLICATION = 32512
IDM_OPEN, IDM_AUTOSTART, IDM_EXIT = 1001, 1002, 1003

STARTUP_DIR = (
    Path(os.environ.get("APPDATA", ""))
    / r"Microsoft\Windows\Start Menu\Programs\Startup"
)
AUTOSTART_LNK = STARTUP_DIR / "AI Conversation Hub.lnk"


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.DWORD), ("hWnd", wt.HWND), ("uID", wt.UINT),
        ("uFlags", wt.UINT), ("uCallbackMessage", wt.UINT), ("hIcon", wt.HICON),
        ("szTip", wt.WCHAR * 128), ("dwState", wt.DWORD), ("dwStateMask", wt.DWORD),
        ("szInfo", wt.WCHAR * 256), ("uVersion", wt.UINT),
        ("szInfoTitle", wt.WCHAR * 64), ("dwInfoFlags", wt.DWORD),
        ("guidItem", ctypes.c_byte * 16), ("hBalloonIcon", wt.HICON),
    ]


WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wt.UINT), ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
        ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
        ("hCursor", wt.HANDLE), ("hbrBackground", wt.HANDLE),
        ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR),
    ]


def normalize_url(value: str) -> str:
    return str(value or DEFAULT_URL).rstrip("/")


def health_url(url: str) -> bool:
    try:
        with urllib.request.urlopen(normalize_url(url) + "/api/health", timeout=1.5) as response:
            payload = json.loads(response.read().decode("utf-8"))
            return response.status == 200 and payload.get("app") == "AIConversationHub"
    except (OSError, ValueError):
        return False


def _port_in_use(url: str) -> bool:
    parsed = urllib.parse.urlsplit(normalize_url(url))
    try:
        port = parsed.port or 80
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind((parsed.hostname or "127.0.0.1", port))
        return False
    except (OSError, ValueError):
        return True


def remembered_url() -> str:
    try:
        payload = json.loads(INSTANCE_PATH.read_text(encoding="utf-8"))
        port = int(payload.get("port") or 0)
        if 1 <= port <= 65535:
            return f"http://127.0.0.1:{port}"
    except (OSError, TypeError, ValueError):
        pass
    return ""


def discover_running_url() -> str:
    candidates = [remembered_url(), *(f"http://127.0.0.1:{p}" for p in range(8765, 8796))]
    for candidate in dict.fromkeys(value for value in candidates if value):
        if _port_in_use(candidate) and health_url(candidate):
            return normalize_url(candidate)
    return ""


def source_launch_command() -> tuple[list[str], Path]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--no-open"], Path(sys.executable).resolve().parent
    pyw = Path(sys.executable).with_name("pythonw.exe")
    executable = pyw if pyw.is_file() else Path(sys.executable)
    return [str(executable), str(SOURCE_DIR / "desktop_app.py"), "--no-open"], SOURCE_DIR


def ensure_server(preferred_url: str = "") -> str:
    for candidate in (preferred_url, discover_running_url()):
        if candidate and health_url(candidate):
            return normalize_url(candidate)
    command, cwd = source_launch_command()
    subprocess.Popen(command, cwd=str(cwd), creationflags=DETACHED_PROCESS)
    deadline = time.time() + 12
    while time.time() < deadline:
        candidate = discover_running_url()
        if candidate:
            return candidate
        time.sleep(0.2)
    return normalize_url(preferred_url or DEFAULT_URL)


def autostart_on() -> bool:
    return AUTOSTART_LNK.is_file()


def _powershell_literal(value: str | Path) -> str:
    return str(value).replace("'", "''")


def set_autostart(on: bool) -> None:
    command, cwd = source_launch_command()
    target = command[0]
    arguments = subprocess.list2cmdline(command[1:])
    if on:
        STARTUP_DIR.mkdir(parents=True, exist_ok=True)
        script = (
            "$ws=New-Object -ComObject WScript.Shell;"
            f"$s=$ws.CreateShortcut('{_powershell_literal(AUTOSTART_LNK)}');"
            f"$s.TargetPath='{_powershell_literal(target)}';"
            f"$s.Arguments='{_powershell_literal(arguments)}';"
            f"$s.WorkingDirectory='{_powershell_literal(cwd)}';$s.Save()"
        )
    else:
        script = f"Remove-Item -LiteralPath '{_powershell_literal(AUTOSTART_LNK)}' -Force -ErrorAction SilentlyContinue"
    subprocess.Popen(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        creationflags=DETACHED_PROCESS,
    )


class Tray:
    def __init__(self, url: str = "", on_exit: Callable[[], None] | None = None) -> None:
        self.url = normalize_url(url or remembered_url() or DEFAULT_URL)
        self.on_exit = on_exit
        self.hwnd = None
        self.nid = None
        self.icon = None
        self.mutex = None
        self.proc = WNDPROC(self._wnd_proc)  # keep callback alive

    def open_center(self) -> None:
        self.url = ensure_server(self.url)
        try:
            os.startfile(self.url)  # type: ignore[attr-defined]
        except OSError:
            pass

    def _exit_app(self) -> None:
        user32.PostQuitMessage(0)
        if self.on_exit:
            threading.Thread(target=self.on_exit, name="hub-tray-shutdown", daemon=True).start()

    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        if msg == WM_TRAYICON:
            if lparam == WM_LBUTTONUP:
                user32.SetTimer(self.hwnd, TIMER_OPEN, 250, None)
            elif lparam == WM_LBUTTONDBLCLK:
                user32.KillTimer(self.hwnd, TIMER_OPEN)
                self.open_center()
            elif lparam == WM_RBUTTONUP:
                self._show_menu()
            return 0
        if msg == WM_TIMER and wparam == TIMER_OPEN:
            user32.KillTimer(self.hwnd, TIMER_OPEN)
            self.open_center()
            return 0
        if msg == WM_TIMER and wparam == TIMER_WATCHDOG:
            if not health_url(self.url):
                self.url = ensure_server(self.url)
            return 0
        if msg == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _show_menu(self) -> None:
        menu = user32.CreatePopupMenu()
        user32.AppendMenuW(menu, 0, IDM_OPEN, "打开 AI 对话中心")
        user32.AppendMenuW(
            menu, MF_CHECKED if autostart_on() else MF_UNCHECKED,
            IDM_AUTOSTART, "开机自动启动",
        )
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, "")
        user32.AppendMenuW(
            menu, 0, IDM_EXIT,
            "退出 AI 对话中心" if self.on_exit else "退出托盘",
        )
        pos = wt.POINT()
        user32.GetCursorPos(ctypes.byref(pos))
        user32.SetForegroundWindow(self.hwnd)
        cmd = user32.TrackPopupMenu(
            menu, TPM_RIGHTALIGN | TPM_RETURNCMD | TPM_NONOTIFY,
            pos.x, pos.y, 0, self.hwnd, None,
        )
        if cmd == IDM_OPEN:
            self.open_center()
        elif cmd == IDM_AUTOSTART:
            set_autostart(not autostart_on())
        elif cmd == IDM_EXIT:
            self._exit_app()
        user32.DestroyMenu(menu)

    def run(self) -> bool:
        self.mutex = kernel32.CreateMutexW(None, False, "AIConversationHubTray")
        if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
            return False

        wc = WNDCLASSW()
        wc.lpfnWndProc = self.proc
        wc.hInstance = kernel32.GetModuleHandleW(None)
        wc.lpszClassName = "AIHubTrayMsgClass"
        user32.RegisterClassW(ctypes.byref(wc))
        self.hwnd = user32.CreateWindowExW(
            0, "AIHubTrayMsgClass", "AIHubTray", 0, 0, 0, 0, 0,
            wt.HWND(-3), None, wc.hInstance, None,
        )

        self.nid = NOTIFYICONDATAW()
        self.nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        self.nid.hWnd = self.hwnd
        self.nid.uID = 1
        self.nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        self.nid.uCallbackMessage = WM_TRAYICON
        icon_path = RESOURCE_DIR / "packaging" / "app_icon.ico"
        if icon_path.is_file():
            self.icon = user32.LoadImageW(None, str(icon_path), 1, 0, 0, 0x0010 | 0x0040)
        self.nid.hIcon = self.icon or user32.LoadIconW(None, IDI_APPLICATION)
        self.nid.szTip = "AI 对话中心 · 单击打开"
        shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(self.nid))
        user32.SetTimer(self.hwnd, TIMER_WATCHDOG, 15000, None)

        try:
            msg = wt.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self.nid))
            if self.icon:
                user32.DestroyIcon(self.icon)
            if self.mutex:
                kernel32.CloseHandle(self.mutex)
        return True


def start_tray_thread(url: str, on_exit: Callable[[], None]) -> threading.Thread:
    tray = Tray(url, on_exit)
    thread = threading.Thread(target=tray.run, name="hub-system-tray", daemon=True)
    thread.start()
    return thread


def main() -> None:
    Tray(discover_running_url()).run()


if __name__ == "__main__":
    main()
