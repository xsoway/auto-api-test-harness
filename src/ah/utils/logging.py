"""harness 的结构化日志。

日志器在根 ``ah`` logger 下创建。``configure_logging()`` 安装一个控制台
处理器以及一个滚动文件处理器（``artifacts/logs/ah.log``），使用稳定、
机器友好的字段格式。无第三方依赖。
"""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_ROOT = "ah"
_default_dir = Path(os.getenv("AH_LOG_DIR", "artifacts/logs"))


def _formatter() -> logging.Formatter:
    return logging.Formatter(
        fmt="%(asctime)s.%(msecs)03dZ %(levelname)-7s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


def configure_logging(
    *,
    log_dir: str | Path | None = None,
    console: bool = True,
    level: int = logging.INFO,
) -> None:
    """在根 ``ah`` logger 上安装处理器（幂等）。

    ``log_dir`` 默认取 ``AH_LOG_DIR`` 环境变量或 ``artifacts/logs``。
    """
    log_dir_path = Path(log_dir) if log_dir is not None else _default_dir
    root = logging.getLogger(_ROOT)
    if getattr(root, "_ah_configured", False):
        return
    root.setLevel(level)
    root.propagate = False
    fmt = _formatter()

    file_path = log_dir_path / "ah.log"
    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(file_path, maxBytes=5 * 1024 * 1024, backupCount=3)
        fh.setFormatter(fmt)
        fh.setLevel(level)
        root.addHandler(fh)
    except OSError as exc:  # 日志目录不可写——降级为仅控制台
        print(f"[ah] warning: cannot open log file {file_path}: {exc}", file=sys.stderr)

    if console:
        ch = logging.StreamHandler()
        ch.setFormatter(fmt)
        ch.setLevel(level)
        root.addHandler(ch)

    root._ah_configured = True  # type: ignore[attr-defined]


def get_logger(name: str) -> logging.Logger:
    """返回 logger ``ah.<name>``（当 ``name`` 为空时创建/返回根 logger）。"""
    if not name:
        return logging.getLogger(_ROOT)
    return logging.getLogger(f"{_ROOT}.{name}")
