"""统一日志配置模块。

用法:
    # main.py 入口处调用一次
    from logging_config import setup
    setup(debug=True, log_file="/tmp/app.log")

    # 其他模块
    from logging_config import get_logger
    logger = get_logger(__name__)
    logger.info("message")
"""

import logging
import sys

_initialized = False


def setup(debug: bool = False, log_file: str | None = None) -> None:
    """配置根 logger（幂等，可安全多次调用）。"""
    global _initialized
    if _initialized:
        return
    _initialized = True

    root = logging.getLogger()
    root.setLevel(logging.DEBUG if debug else logging.INFO)

    # 清除已有的 handler（如 utils.py 中 basicConfig 添加的）
    if root.handlers:
        for h in root.handlers[:]:
            root.removeHandler(h)

    # 抑制嘈杂第三方库的 DEBUG 日志（websockets 会输出原始二进制帧数据）
    for lib in ("websockets", "urllib3", "asyncio", "httpx", "httpcore", "openai",
                 "video.foxglove_client", "audio.interrupt_detector", "tts.protocols"):
        logging.getLogger(lib).setLevel(logging.WARNING)
    # TTS 模块允许 DEBUG（受根 logger 级别控制），确保 WARNING/ERROR 不被压制
    for lib in ("tts.bytedance_tts",):
        logging.getLogger(lib).setLevel(logging.DEBUG)

    # 控制台 handler：保留原始的 emoji + [tag] 风格，不带时间戳
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(console)

    # 文件 handler（可选）：带时间戳和级别，便于事后排查
    if log_file:
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(
            logging.Formatter(
                "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        root.addHandler(file_handler)


def get_logger(name: str) -> logging.Logger:
    """获取指定模块的 logger，无需重复配置。"""
    return logging.getLogger(name)