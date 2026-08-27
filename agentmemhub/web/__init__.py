"""AgentMemHub Web 模块（可选启用）。

提供本地 Web 页面加载统一会话库：
    python -m agentmemhub serve --port 8086 --open

依赖（可选安装）：uv pip install -e ".[web]"
"""
from __future__ import annotations

import threading
import webbrowser
from pathlib import Path
from typing import Optional


def run_server(port: int = 8086, open_browser: bool = False,
               db: Optional[str] = None, host: str = "127.0.0.1") -> None:
    """启动 Web 服务并阻塞运行。fastapi/uvicorn 未安装时给出清晰提示。"""
    try:
        import uvicorn  # noqa: F401
        from agentmemhub.web.app import create_app
    except ImportError as e:
        raise SystemExit(
            "缺少 Web 依赖（fastapi/uvicorn）。请执行：\n"
            '    uv pip install -e ".[web]"\n'
            f"原始错误：{e}"
        )

    app = create_app(Path(db) if db else None)
    url = f"http://{host}:{port}/"
    print(f"AgentMemHub Web 已启动: {url}   (API 文档: {url}api/docs)")
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=host, port=port, log_level="warning")