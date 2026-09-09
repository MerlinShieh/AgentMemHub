"""日志单点配置：统一 logs/ 目录、按程序分文件、UTF-8、Rotating。

用法：log = get_logger("ingest", log_dir)  →  logs/asrag-ingest.log
测试通过 conftest 注入 tmp_path 隔离，绝不写真实 logs/。
"""
from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

_MAX_BYTES = 5 * 1024 * 1024
_BACKUP_COUNT = 3
_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"


def get_logger(
    component: str,
    log_dir: Path | str,
    level: int = logging.INFO,
    console: bool = True,
) -> logging.Logger:
    """返回挂接了 logs/asrag-<component>.log 的 logger。重复调用幂等。"""
    name = f"asrag.{component}"
    logger = logging.getLogger(name)
    log_dir = Path(log_dir)
    # 幂等 = 同组件同目录；换目录（测试隔离 tmp_path）则重建 handler
    if getattr(logger, "_asrag_dir", None) == str(log_dir.resolve()):
        return logger
    for h in list(logger.handlers):
        logger.removeHandler(h)
        if isinstance(h, logging.FileHandler):
            h.close()
    logger.setLevel(level)
    logger.propagate = False

    log_dir.mkdir(parents=True, exist_ok=True)
    fh = logging.handlers.RotatingFileHandler(
        log_dir / f"asrag-{component}.log",
        maxBytes=_MAX_BYTES,
        backupCount=_BACKUP_COUNT,
        encoding="utf-8",
    )
    fh.setFormatter(logging.Formatter(_FORMAT))
    logger.addHandler(fh)

    if console:
        ch = logging.StreamHandler()
        ch.setFormatter(logging.Formatter(_FORMAT))
        logger.addHandler(ch)

    logger._asrag_dir = str(log_dir.resolve())  # type: ignore[attr-defined]
    return logger
