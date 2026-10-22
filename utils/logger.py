"""统一日志入口。"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

ROOT_LOGGER_NAME = "paper-polish"
_root_configured = False


def setup_logging(
    level: int | str = logging.INFO,
    *,
    log_file: str | Path | None = None,
    console: bool = True,
) -> None:
    global _root_configured
    ensure_utf8_stdio()  # 非 CLI 入口（如 unittest）也要防 GBK 控制台乱码
    logger = logging.getLogger(ROOT_LOGGER_NAME)
    logger.setLevel(level if isinstance(level, int) else getattr(logging, str(level).upper()))
    logger.propagate = False

    if _root_configured:
        return
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    if console:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(path, encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    _root_configured = True


def get_logger(name: str = "") -> logging.Logger:
    if not _root_configured:
        setup_logging()
    full_name = f"{ROOT_LOGGER_NAME}.{name}" if name else ROOT_LOGGER_NAME
    return logging.getLogger(full_name)


def ensure_utf8_stdio() -> None:
    """Windows 控制台/重定向场景下把 stdout/stderr 切到 UTF-8，避免 GBK 编码崩溃。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8")
            except (ValueError, OSError):  # pragma: no cover - 已关闭的流
                pass
