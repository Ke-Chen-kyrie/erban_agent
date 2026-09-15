"""统一日志配置 —— get_logger() + setup()。"""

import logging
import sys

# 模块级日志级别覆盖（调试时用）
_MODULE_LEVELS: dict[str, int] = {
    # "agent.router": logging.DEBUG,
    # "minicpmo": logging.DEBUG,
}

_configured = False


def get_logger(name: str) -> logging.Logger:
    """获取 logger，但不在库导入时修改应用的全局日志配置。"""
    logger = logging.getLogger(name)

    # 应用模块级覆盖
    for prefix, level in _MODULE_LEVELS.items():
        if name == prefix or name.startswith(prefix + "."):
            logger.setLevel(level)
            break

    return logger


def setup(debug: bool = False, log_file: str | None = None) -> None:
    """配置 root logger。

    Args:
        debug: True → DEBUG 级别，False → INFO 级别
        log_file: 日志文件路径，None 则仅输出到控制台
    """
    global _configured

    level = logging.DEBUG if debug else logging.INFO
    fmt = logging.Formatter(
        "[%(asctime)s] %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(level)

    # 清除已有 handler，避免重复
    for h in root.handlers[:]:
        root.removeHandler(h)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    console.setLevel(level)
    root.addHandler(console)

    if log_file:
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(fmt)
        fh.setLevel(level)
        root.addHandler(fh)

    _configured = True

    # 抑制第三方库的 HTTP 请求日志
    for _name in ("httpx", "httpx2", "httpcore", "httpcore2",
                  "websockets", "openai", "asyncio"):
        logging.getLogger(_name).setLevel(logging.WARNING)
    # TTS 协议层二进制帧日志太吵
    logging.getLogger("tts.protocols").setLevel(logging.INFO)
