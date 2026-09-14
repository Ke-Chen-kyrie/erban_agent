"""线程内运行异步协程的安全工具函数。

解决 daemon 线程中创建私有 asyncio event loop 时缺少清理的问题：
确保退出时取消所有 pending tasks 并关闭 loop。
"""

import asyncio
from typing import Coroutine, TypeVar

T = TypeVar("T")


def run_async_in_thread(coro: Coroutine, *, timeout: float | None = None) -> T:
    """在线程中安全运行异步协程，确保退出时清理任务和关闭 loop。

    Args:
        coro: 要运行的协程对象。
        timeout: 可选超时（秒），超时后取消协程。

    Returns:
        协程的返回值。

    Raises:
        TimeoutError: 如果超时。
        Exception: 协程内部抛出的异常会原样传播。
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        if timeout is not None:
            return loop.run_until_complete(asyncio.wait_for(coro, timeout=timeout))
        return loop.run_until_complete(coro)
    finally:
        # 取消所有 pending tasks
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(
                asyncio.gather(*pending, return_exceptions=True)
            )
        loop.close()
