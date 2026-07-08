"""
Colored Formatter - 彩色日志格式化器

使用 colorama 实现日志级别的彩色输出。
"""

from __future__ import annotations

import logging
import sys

from colorama import Fore, Style, init

# 初始化 colorama（Windows 兼容）
# autoreset=True:每次写入后自动重置颜色,避免颜色码"溢出"影响后续输出。
init(autoreset=True)


class ColoredFormatter(logging.Formatter):
    """
    彩色日志格式化器

    根据日志级别自动着色，非 TTY 环境自动降级为纯文本。

    颜色映射:
        DEBUG: CYAN
        INFO: GREEN
        WARNING: YELLOW
        ERROR: RED
        CRITICAL: RED + BOLD
    """

    LEVEL_COLORS = {
        logging.DEBUG: Fore.CYAN,
        logging.INFO: Fore.GREEN,
        logging.WARNING: Fore.YELLOW,
        logging.ERROR: Fore.RED,
        logging.CRITICAL: Fore.RED + Style.BRIGHT,
    }

    def __init__(self, fmt: str | None = None, datefmt: str | None = None):
        super().__init__(fmt, datefmt)
        # 启动时判定是否连接到 TTY(终端);非 TTY(如重定向到文件/管道)则不着色,输出纯文本。
        self._is_tty = sys.stdout.isatty()

    def format(self, record: logging.LogRecord) -> str:
        """格式化日志记录，添加颜色"""
        # 保存原始 levelname
        # 只临时给 levelname 包上颜色码用于本次输出,格式化后立即还原,
        # 避免污染同一 record 被其他 handler 复用时的字段。
        original_levelname = record.levelname

        if self._is_tty:
            # 添加颜色
            color = self.LEVEL_COLORS.get(record.levelno, "")
            record.levelname = f"{color}{record.levelname}{Style.RESET_ALL}"

        result = super().format(record)

        # 恢复原始 levelname
        record.levelname = original_levelname

        return result
