from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser

from app_paths import CONFIG_PATH, DATA_DIR


INSTANCE_PATH = DATA_DIR / "instance.json"


def health_payload(port: int) -> dict:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=0.75) as response:
            payload = json.loads(response.read().decode("utf-8"))
            if (
                response.status == 200
                and payload.get("app") == "AIConversationHub"
                and str(payload.get("data_dir", "")).casefold() == str(DATA_DIR).casefold()
            ):
                return payload
    except (OSError, ValueError, urllib.error.URLError):
        pass
    return {}


def health(port: int) -> bool:
    return bool(health_payload(port))


def remembered_port() -> int | None:
    try:
        payload = json.loads(INSTANCE_PATH.read_text(encoding="utf-8"))
        port = int(payload.get("port", 0))
        return port if 1 <= port <= 65535 else None
    except (OSError, ValueError, TypeError):
        return None


def remember_port(port: int) -> None:
    try:
        INSTANCE_PATH.parent.mkdir(parents=True, exist_ok=True)
        INSTANCE_PATH.write_text(
            json.dumps({"port": port}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass


def running_port() -> int | None:
    """在默认端口段里找一个已经在跑、且数据目录一致的实例；没有则返回 None。"""
    for port in range(8765, 8796):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                pass
            else:
                continue
        if health(port):
            return port
    return None


def free_port() -> int:
    """找一个真正空闲、可以绑定的端口。被占用的端口一律跳过。"""
    for port in range(8765, 8796):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def ensure_firewall_allowed() -> None:
    """首次运行时添加 Windows 防火墙入站规则（仅 Windows，仅首次弹 UAC）。"""
    if sys.platform != "win32":
        return
    exe = sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__)
    rule_name = "AIConversationHub (Inbound)"

    # 检查规则是否已存在。
    # netsh show rule name=X：规则存在 returncode==0，否则 !=0（最可靠信号）。
    # 不依赖 stdout 文本匹配——中文 Windows 下 netsh 输出是 GBK，且 PyInstaller
    # exe 环境下子进程编码行为可能与开发机不同，靠文本匹配会误判。
    try:
        check = subprocess.run(
            ["netsh", "advfirewall", "firewall", "show", "rule", f"name={rule_name}"],
            capture_output=True, creationflags=0x08000000,  # CREATE_NO_WINDOW
        )
        if check.returncode == 0:
            return  # 规则已存在，跳过提权
    except (OSError, subprocess.SubprocessError):
        pass

    # 用 ctypes ShellExecuteW 提权，避免 VBS 引号转义问题
    import ctypes
    params = f'advfirewall firewall add rule name="{rule_name}" dir=in action=allow program="{exe}" enable=yes profile=any'
    try:
        ctypes.windll.shell32.ShellExecuteW(
            None, "runas", "netsh", params, None, 0  # 0 = SW_HIDE
        )
    except (OSError, Exception):
        pass
    time.sleep(3)


def ensure_initial_source_config() -> None:
    if CONFIG_PATH.is_file():
        return
    from repair_sources import repair

    repair(apply=True)


def ensure_server_started(*, wait_for_index: bool = False) -> tuple[int, threading.Thread | None]:
    from server import run_server

    ensure_initial_source_config()
    port = remembered_port()
    if not port or not health(port):
        port = running_port()

    server_thread = None
    if not port:
        port = free_port()
        server_thread = threading.Thread(
            target=run_server,
            args=(port,),
            kwargs={"open_browser": False, "enable_tray": False},
            daemon=True,
        )
        server_thread.start()
        for _ in range(120):
            if health(port):
                break
            if not server_thread.is_alive():
                raise RuntimeError(f"服务线程在启动过程中退出（端口 {port}）。")
            time.sleep(0.2)
        else:
            raise RuntimeError("AI 对话中心未能启动。")
        remember_port(port)

    if wait_for_index:
        for _ in range(600):
            payload = health_payload(port)
            index = payload.get("index") if isinstance(payload, dict) else {}
            if isinstance(index, dict) and index.get("ready"):
                break
            if isinstance(index, dict) and index.get("status") == "error":
                raise RuntimeError(str(index.get("error") or "索引初始化失败"))
            time.sleep(0.2)
        else:
            raise RuntimeError("AI 对话中心索引初始化超时。")
    return port, server_thread


def launch(*, open_browser: bool = True) -> None:
    port, server_thread = ensure_server_started()
    if open_browser:
        webbrowser.open(f"http://127.0.0.1:{port}/")

    # 复用已有实例时 server_thread 为 None：浏览器已打开，本进程直接退出即可。
    # 自己启动的实例才需要保持主进程存活（server 线程是 daemon）。
    if server_thread:
        try:
            server_thread.join()
        except KeyboardInterrupt:
            pass


def main() -> None:
    try:
        launch()
    except Exception as exc:  # 让双击失败时窗口停住、能看到原因
        _show_error(exc)


def _show_error(exc: Exception) -> None:
    """启动失败时显示错误信息：有控制台走 print，无控制台(--windowed)弹图形对话框。"""
    msg = f"启动失败：{exc}\n\n排查提示：\n  1. 请先把整个文件夹从压缩包完整解压后再运行；\n  2. 若被杀毒软件拦截，请添加信任；\n  3. 确认程序文件完整（_internal 文件夹与主程序在同一目录）。"
    if sys.stdout:  # 有控制台（Windows 控制台版 / 源码运行）
        print("\n" + msg)
        try:
            input("\n按回车键退出…")
        except (EOFError, KeyboardInterrupt):
            pass
    elif sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, msg, "AI Conversation Hub 启动失败", 0x10)
        except Exception:
            pass
    else:  # 无控制台（macOS --windowed .app）：弹图形对话框，否则用户看不到任何错误
        try:
            import subprocess
            escaped = msg.replace('"', '\\"').replace("\\", "\\\\")
            subprocess.run(
                ["osascript", "-e",
                 f'display dialog "{escaped}" with title "AI Conversation Hub 启动失败" buttons {{"退出"}} default button 1 with icon stop'],
                timeout=120,
            )
        except Exception:
            pass  # 连 osascript 都失败时，只能静默退出


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AI Conversation Hub desktop launcher")
    parser.add_argument("--no-open", action="store_true", help="Do not open the browser automatically")
    cli_args = parser.parse_args()
    try:
        launch(open_browser=not cli_args.no_open)
    except Exception as exc:
        _show_error(exc)
